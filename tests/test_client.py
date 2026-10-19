"""客户端与 CLI 的端到端测试：起一个真的本地 HTTP 服务，不 mock urllib。

这样测到的是实际的多部分表单、真实的请求头、真实的流式下载 ——
而不是「mock 说它收到了」。
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from image2ppt import (
    APIError,
    AuthenticationError,
    Client,
    InsufficientCredits,
    JobFailed,
    JobNotReady,
    PDFConfirmationRequired,
    mint_token,
)
from image2ppt import cli


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # noqa: D401 - 静音测试日志
        pass

    def _handle(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        status, headers, payload = self.server.app(  # type: ignore[attr-defined]
            self.command, self.path, dict(self.headers), body)
        if isinstance(payload, (dict, list)):
            payload = json.dumps(payload).encode("utf-8")
        elif isinstance(payload, str):
            payload = payload.encode("utf-8")
        self.send_response(status)
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    do_GET = _handle
    do_POST = _handle


class MockServer:
    def __init__(self, app):
        self.app = app
        self.httpd = None
        self.thread = None

    def __enter__(self):
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.httpd.app = self.app  # type: ignore[attr-defined]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        return self

    def __exit__(self, *exc_info):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)


def _form_field(body: bytes, name: str) -> bytes:
    marker = f'name="{name}"'.encode()
    start = body.index(marker)
    value_start = body.index(b"\r\n\r\n", start) + 4
    value_end = body.index(b"\r\n", value_start)
    return body[value_start:value_end]


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.src = Path(self.tmp.name) / "figure.png"
        self.src.write_bytes(b"\x89PNG\r\n\x1a\nfake-image")

    def _client(self, base_url, **kwargs):
        return Client(token="ppt_test_token", base_url=base_url, **kwargs)

    def test_me_sends_bearer_token(self):
        seen = {}

        def app(method, path, headers, body):
            seen["method"] = method
            seen["path"] = path
            seen["auth"] = headers.get("Authorization")
            return 200, {}, {"ok": True, "email": "a@b.com", "credits": 3}

        with MockServer(app) as server:
            with self._client(server.base_url) as client:
                data = client.me()

        self.assertEqual(data["credits"], 3)
        self.assertEqual(seen["path"], "/api/v1/me")
        self.assertEqual(seen["auth"], "Bearer ppt_test_token")

    def test_token_file_is_used_when_env_is_missing(self):
        cfg = Path(self.tmp.name) / "cfg"
        (cfg / "image2ppt").mkdir(parents=True)
        (cfg / "image2ppt" / "token").write_text("ppt_from_file\n", encoding="utf-8")
        seen = {}

        def app(method, path, headers, body):
            seen["auth"] = headers.get("Authorization")
            return 200, {}, {"ok": True, "email": "a@b.com", "credits": 1}

        with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(cfg)}, clear=False):
            os.environ.pop("IMAGEPPT_TOKEN", None)
            os.environ.pop("IMG2PPT_TOKEN", None)
            with MockServer(app) as server:
                Client(base_url=server.base_url).me()

        self.assertEqual(seen["auth"], "Bearer ppt_from_file")

    def test_convert_builds_multipart(self):
        seen = {}

        def app(method, path, headers, body):
            seen["path"] = path
            seen["ctype"] = headers.get("Content-Type", "")
            seen["body"] = body
            return 200, {}, {"ok": True, "job_id": "42", "credits": 2,
                             "pages": 1, "total_pages": 1}

        with MockServer(app) as server:
            result = self._client(server.base_url).convert(self.src, ratio="16:9")

        self.assertEqual(result.job_id, "42")
        self.assertEqual(result.credits, 2)
        self.assertTrue(seen["ctype"].startswith("multipart/form-data; boundary="))
        self.assertIn(b'filename="figure.png"', seen["body"])
        self.assertEqual(_form_field(seen["body"], "ratio"), b"16:9")
        self.assertIn(b"fake-image", seen["body"])

    def test_pdf_requires_confirmation(self):
        def app(method, path, headers, body):
            return 200, {}, {"ok": False, "need_confirm": True, "total_pages": 5,
                             "credits": 3, "pages_to_convert": 3,
                             "msg": "本次共 5 页，将一次扣 3 积分，确认转换？"}

        with MockServer(app) as server:
            with self.assertRaises(PDFConfirmationRequired) as ctx:
                self._client(server.base_url).convert(self.src)

        self.assertEqual(ctx.exception.total_pages, 5)
        self.assertEqual(ctx.exception.credits, 3)
        self.assertEqual(ctx.exception.pages_to_convert, 3)
        self.assertIn("5 页", str(ctx.exception))

    def test_confirm_pages_is_sent_back(self):
        seen = {}

        def app(method, path, headers, body):
            seen["body"] = body
            return 200, {}, {"ok": True, "job_id": "7", "credits": 0,
                             "pages": 3, "total_pages": 5}

        with MockServer(app) as server:
            result = self._client(server.base_url).convert(self.src, confirm_pages=3)

        self.assertEqual(result.pages, 3)
        self.assertEqual(_form_field(seen["body"], "confirm_pages"), b"3")

    def test_wait_retries_transient_errors_and_reports_progress(self):
        state = {"polls": 0}

        def app(method, path, headers, body):
            if path.endswith("/api/v1/jobs/9"):
                state["polls"] += 1
                if state["polls"] == 1:
                    return 503, {}, {"detail": "服务重启中"}
                if state["polls"] == 2:
                    return 200, {}, {"ok": True, "job": {
                        "id": 9, "status": "pending", "stage": "正在读图…",
                        "preview": 1, "round": 0, "has_file": False}}
                return 200, {}, {"ok": True, "job": {
                    "id": 9, "status": "ok", "stage": "完成",
                    "preview": 3, "round": 2, "rounds": 2, "has_file": True}}
            return 404, {}, {"detail": "not found"}

        stages = []
        with MockServer(app) as server:
            job = self._client(server.base_url, poll_interval=0.01).wait(
                "9", timeout=5,
                on_progress=lambda j, elapsed: stages.append(j.stage))

        self.assertEqual(job.status, "ok")
        self.assertEqual(job.round, 2)
        self.assertEqual(stages, ["正在读图…", "完成"])

    def test_wait_raises_when_job_failed(self):
        def app(method, path, headers, body):
            return 200, {}, {"ok": True, "job": {
                "id": 3, "status": "failed", "error": "源图损坏"}}

        with MockServer(app) as server:
            with self.assertRaises(JobFailed) as ctx:
                self._client(server.base_url).wait("3", timeout=5)

        self.assertIn("源图损坏", str(ctx.exception))
        self.assertEqual(ctx.exception.job.id, "3")

    def test_download_streams_pptx_and_uses_server_filename(self):
        pptx = b"PK\x03\x04" + b"x" * 5000

        def app(method, path, headers, body):
            self.assertEqual(path, "/api/v1/jobs/5/pptx")
            return 200, {
                "Content-Type": "application/vnd.openxmlformats-officedocument."
                                "presentationml.presentation",
                "Content-Disposition": "attachment; filename*=UTF-8''result.pptx",
            }, pptx

        out_dir = Path(self.tmp.name) / "downloads"
        out_dir.mkdir()
        with MockServer(app) as server:
            path = self._client(server.base_url).download("5", out_dir)

        self.assertEqual(path, out_dir / "result.pptx")
        self.assertEqual(path.read_bytes(), pptx)

    def test_insufficient_credits_carries_recharge_url(self):
        def app(method, path, headers, body):
            return 402, {}, {"detail": "积分不足，请先充值",
                             "recharge_url": "https://image2ppt.asia/"}

        with MockServer(app) as server:
            with self.assertRaises(InsufficientCredits) as ctx:
                self._client(server.base_url).convert(self.src)

        self.assertEqual(ctx.exception.recharge_url, "https://image2ppt.asia/")
        self.assertEqual(ctx.exception.status_code, 402)

    def test_invalid_token_is_authentication_error(self):
        def app(method, path, headers, body):
            return 401, {}, {"detail": "令牌无效或已吊销，请到站里重新签发"}

        with MockServer(app) as server:
            with self.assertRaises(AuthenticationError):
                self._client(server.base_url).me()

    def test_download_before_ready_is_job_not_ready(self):
        def app(method, path, headers, body):
            return 404, {}, {"detail": "成品还没准备好"}

        with MockServer(app) as server:
            with self.assertRaises(JobNotReady):
                self._client(server.base_url).download("8")

    def test_mint_token_posts_credentials_without_bearer(self):
        seen = {}

        def app(method, path, headers, body):
            seen["path"] = path
            seen["auth"] = headers.get("Authorization")
            seen["json"] = json.loads(body)
            return 200, {}, {"ok": True, "token": "ppt_new_token",
                             "prefix": "ppt_new_", "credits": 3}

        with MockServer(app) as server:
            token = mint_token("a@b.com", "secret", name="ci",
                               base_url=server.base_url)

        self.assertEqual(token, "ppt_new_token")
        self.assertIsNone(seen["auth"])
        self.assertEqual(seen["path"], "/api/auth/token")
        self.assertEqual(seen["json"], {"email": "a@b.com", "password": "secret",
                                        "name": "ci"})


class CLITests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.src = Path(self.tmp.name) / "figure.png"
        self.src.write_bytes(b"\x89PNG\r\n\x1a\nfake-image")
        self.env = mock.patch.dict(os.environ, {
            "NO_PROXY": "127.0.0.1,localhost",
            "no_proxy": "127.0.0.1,localhost",
        })
        self.env.start()
        self.addCleanup(self.env.stop)

    def _run(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_convert_end_to_end(self):
        pptx = b"PK\x03\x04" + b"y" * 6000

        def app(method, path, headers, body):
            if path == "/api/v1/convert":
                return 200, {}, {"ok": True, "job_id": "11", "credits": 2,
                                 "pages": 1, "total_pages": 1}
            if path == "/api/v1/jobs/11":
                return 200, {}, {"ok": True, "job": {
                    "id": 11, "status": "ok", "stage": "完成",
                    "round": 2, "rounds": 2, "has_file": True}}
            if path == "/api/v1/jobs/11/pptx":
                return 200, {"Content-Disposition": 'attachment; filename="x.pptx"'}, pptx
            return 404, {}, {"detail": "not found"}

        out_path = Path(self.tmp.name) / "result.pptx"
        with MockServer(app) as server:
            code, out, err = self._run([
                "--base", server.base_url, "--token", "ppt_x",
                "convert", str(self.src), "-o", str(out_path),
                "--poll", "0.01",
            ])

        self.assertEqual(code, 0, err)
        self.assertEqual(out_path.read_bytes(), pptx)
        self.assertIn("成品已保存", out)
        self.assertIn("作业 #11 已提交", out)

    def test_auth_error_exit_code(self):
        def app(method, path, headers, body):
            return 401, {}, {"detail": "令牌无效"}

        with MockServer(app) as server:
            code, out, err = self._run(["--base", server.base_url,
                                        "--token", "bad", "me"])

        self.assertEqual(code, cli.EXIT_AUTH)
        self.assertIn("令牌无效", err)

    def test_missing_token_exit_code(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("IMAGEPPT_TOKEN", None)
            os.environ.pop("IMG2PPT_TOKEN", None)
            code, out, err = self._run(["--base", "http://127.0.0.1:1", "me"])

        self.assertEqual(code, cli.EXIT_AUTH)
        self.assertIn("缺少 API 令牌", err)


if __name__ == "__main__":
    unittest.main()
