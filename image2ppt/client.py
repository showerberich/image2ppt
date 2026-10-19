"""image2ppt 的 Python 客户端：把图片 / PDF 交给 image2ppt.asia，拿回可编辑 PPTX。

这个文件**只依赖 Python 标准库**。它不包含任何图像识别或 PPT 渲染逻辑 ——
那些都在服务端。这里只做四件事：

    上传 → 轮询进度 → 下载成品 → 把失败翻译成能行动的异常

接口契约见 https://image2ppt.asia 和仓库 README 的「HTTP API」一节。
"""

from __future__ import annotations

import json
import mimetypes
import os
import re
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

DEFAULT_BASE_URL = "https://image2ppt.asia"
DEFAULT_TIMEOUT = 120.0          # 单次 HTTP 请求
DEFAULT_POLL_INTERVAL = 20.0     # 服务端每 5 秒扫一次作业目录，20 秒问一次足够
DEFAULT_WAIT_TIMEOUT = 3600.0    # 实测单张 20–40 分钟，默认给到 1 小时

USER_AGENT = "image2ppt-python/1.0"
_TOKEN_ENV = ("IMAGEPPT_TOKEN", "IMG2PPT_TOKEN")
_BASE_ENV = ("IMAGEPPT_BASE_URL", "IMG2PPT_API")
_TOKEN_FILE_ENV = "IMAGEPPT_TOKEN_FILE"


# ---------------------------------------------------------------------------
# 异常：每一种都对应调用方**能采取的不同行动**
# ---------------------------------------------------------------------------

class Image2PPTError(Exception):
    """所有错误的基类。"""


class NetworkError(Image2PPTError):
    """连不上、超时、DNS 失败。轮询期间可以重试；单次调用则应报给用户。"""


class APIError(Image2PPTError):
    """服务端返回了 4xx / 5xx。`detail` 是给人看的中文原因。"""

    def __init__(self, message: str, *, status_code: Optional[int] = None,
                 payload: Optional[Mapping[str, Any]] = None):
        super().__init__(message)
        self.status_code = status_code
        self.payload = dict(payload or {})
        self.detail = message


class AuthenticationError(APIError):
    """令牌无效、已吊销，或者根本没设置。去站里重新签发一枚。"""


class InsufficientCredits(APIError):
    """402：积分不够。把 `recharge_url` 交给用户，充完直接重试。"""

    @property
    def recharge_url(self) -> str:
        return str(self.payload.get("recharge_url") or DEFAULT_BASE_URL + "/")


class JobNotReady(APIError):
    """404：作业还没跑完，成品 / 中间稿还不存在。"""


class JobFailed(Image2PPTError):
    """服务端把作业判失败了。积分会自动退回。"""

    def __init__(self, message: str, job: "Job"):
        super().__init__(message)
        self.job = job


class ConversionTimeout(Image2PPTError):
    """等待超时。**作业还在服务端继续跑**，不要重新提交（会再扣一次分）。"""

    def __init__(self, message: str, *, job_id: str, job: Optional["Job"] = None):
        super().__init__(message)
        self.job_id = job_id
        self.job = job


class PDFConfirmationRequired(Image2PPTError):
    """多页 PDF 的确认步骤：先在页数 / 积分上达成一致，再扣钱。"""

    def __init__(self, payload: Mapping[str, Any]):
        self.payload = dict(payload)
        self.total_pages = int(payload.get("total_pages") or 0)
        self.credits = int(payload.get("credits") or 0)
        self.pages_to_convert = int(payload.get("pages_to_convert") or 0)
        msg = str(payload.get("msg") or
                  f"共 {self.total_pages} 页，将扣 {self.pages_to_convert} 积分，确认转换？")
        super().__init__(msg)


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Job:
    """服务端作业的只读快照。`stage` 是中文的自然语言进度。"""

    id: str
    status: str
    stage: str = ""
    preview: int = 0
    round: int = 0
    rounds: int = 0
    has_file: bool = False
    filename: str = ""
    error: Optional[str] = None
    pages: Optional[int] = None
    total_pages: Optional[int] = None
    cost: int = 1
    payload: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)

    @classmethod
    def from_payload(cls, d: Mapping[str, Any]) -> "Job":
        return cls(
            id=str(d.get("id", "")),
            status=str(d.get("status") or ""),
            stage=str(d.get("stage") or ""),
            preview=int(d.get("preview") or 0),
            round=int(d.get("round") or 0),
            rounds=int(d.get("rounds") or 0),
            has_file=bool(d.get("has_file")),
            filename=str(d.get("filename") or ""),
            error=d.get("error"),
            pages=d.get("pages"),
            total_pages=d.get("total_pages"),
            cost=int(d.get("cost") or 1),
            payload=dict(d),
        )

    @property
    def done(self) -> bool:
        return self.status == "ok"

    @property
    def failed(self) -> bool:
        return self.status == "failed"


@dataclass(frozen=True)
class ConvertResult:
    """刚提交成功的作业。"""

    job_id: str
    credits: int
    pages: int
    total_pages: int
    payload: Mapping[str, Any] = field(default_factory=dict, repr=False, compare=False)


@dataclass(frozen=True)
class Conversion:
    """完整的转换结果：落盘路径 + 最终作业 + 总耗时。"""

    path: Path
    job: Job
    elapsed: float


ProgressCallback = Callable[[Job, float], None]


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------

def _env_first(names: tuple[str, ...]) -> str:
    for name in names:
        value = (os.environ.get(name) or "").strip()
        if value:
            return value
    return ""


def resolve_base_url(base_url: Optional[str] = None) -> str:
    base = (base_url or _env_first(_BASE_ENV) or DEFAULT_BASE_URL).strip().rstrip("/")
    if not base:
        raise Image2PPTError("base_url 不能为空")
    return base


def default_token_file() -> Path:
    """`image2ppt token --save` 的默认落点（权限 600）。"""
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return Path(base) / "image2ppt" / "token"


def resolve_token(token: Optional[str] = None) -> str:
    value = (token or _env_first(_TOKEN_ENV)).strip()
    if not value:
        path = Path(os.environ.get(_TOKEN_FILE_ENV) or default_token_file()).expanduser()
        try:
            value = path.read_text(encoding="utf-8").strip()
        except OSError:
            value = ""
    if not value:
        raise AuthenticationError(
            "缺少 API 令牌：设置环境变量 IMAGEPPT_TOKEN=ppt_xxx，"
            "或先运行 `image2ppt token --email 你的邮箱 --save` 换取一枚。"
        )
    return value


def _parse_json(raw: bytes) -> Any:
    if not raw:
        return {}
    try:
        return json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None


def _error_from_response(status: int, raw: bytes) -> APIError:
    payload = _parse_json(raw)
    if not isinstance(payload, dict):
        payload = {}
    detail = (payload.get("detail") or payload.get("msg")
              or raw[:300].decode("utf-8", "replace") or f"HTTP {status}")
    detail = str(detail)
    if status == 401:
        return AuthenticationError(detail, status_code=status, payload=payload)
    if status == 402:
        return InsufficientCredits(detail, status_code=status, payload=payload)
    if status == 404 and ("成品" in detail or "中间稿" in detail or "准备好" in detail):
        return JobNotReady(detail, status_code=status, payload=payload)
    return APIError(detail, status_code=status, payload=payload)


def _multipart(fields: Mapping[str, str], source: Path) -> tuple[bytes, str]:
    """构造 multipart/form-data。标准库里没有现成的，为这点事引入依赖不划算。"""
    boundary = "----image2ppt" + secrets.token_hex(16)
    filename = source.name.replace('"', "")
    ctype = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    if filename.lower().endswith(".pdf"):
        ctype = "application/pdf"

    buf = bytearray()
    for key, value in fields.items():
        buf += (
            f'--{boundary}\r\n'
            f'Content-Disposition: form-data; name="{key}"\r\n\r\n'
            f'{value}\r\n'
        ).encode("utf-8")
    buf += (
        f'--{boundary}\r\n'
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        f'Content-Type: {ctype}\r\n\r\n'
    ).encode("utf-8")
    buf += source.read_bytes() + b"\r\n"
    buf += f"--{boundary}--\r\n".encode("utf-8")
    return bytes(buf), f"multipart/form-data; boundary={boundary}"


def _filename_from_headers(headers: Mapping[str, str]) -> Optional[str]:
    cd = headers.get("Content-Disposition") or ""
    m = re.search(r"filename\*\s*=\s*UTF-8''([^;]+)", cd, re.I)
    if m:
        return urllib.parse.unquote(m.group(1).strip().strip('"'))
    m = re.search(r'filename\s*=\s*"([^"]+)"', cd, re.I)
    if m:
        return m.group(1)
    m = re.search(r"filename\s*=\s*([^;]+)", cd, re.I)
    if m:
        return m.group(1).strip().strip('"')
    return None


def _resolve_output(output: Optional[os.PathLike | str], default_name: str) -> Path:
    if output is None:
        return Path(default_name)
    p = Path(output).expanduser()
    raw = str(output)
    if p.is_dir() or raw.endswith(("/", os.sep)):
        p.mkdir(parents=True, exist_ok=True)
        return p / default_name
    return p


def _stream_to_file(response: Any, out: Path) -> Path:
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.name + ".part")
    try:
        with tmp.open("wb") as fh:
            while True:
                chunk = response.read(1 << 20)
                if not chunk:
                    break
                fh.write(chunk)
        os.replace(tmp, out)          # 半截文件不冒充成品
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
    return out


# ---------------------------------------------------------------------------
# 客户端
# ---------------------------------------------------------------------------

class Client:
    """一个很薄的 HTTP 客户端。

    最小用法::

        from image2ppt import Client

        with Client() as c:                       # 读 IMAGEPPT_TOKEN
            result = c.convert_and_wait("figure.png")
            print(result.path)

    令牌只会在请求头里出现，不会被写进日志或产物。
    """

    def __init__(self, token: Optional[str] = None, base_url: Optional[str] = None,
                 timeout: float = DEFAULT_TIMEOUT,
                 poll_interval: float = DEFAULT_POLL_INTERVAL):
        self.token = (token if token is not None else _env_first(_TOKEN_ENV)).strip()
        self.base_url = resolve_base_url(base_url)
        self.timeout = float(timeout)
        self.poll_interval = float(poll_interval)

    # -- 生命周期 ---------------------------------------------------------

    def __enter__(self) -> "Client":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()

    def close(self) -> None:
        """只为 API 对称保留：urllib 每次请求都会自己关连接。"""

    # -- 底层 HTTP --------------------------------------------------------

    def _open(self, method: str, path: str, *, data: Optional[bytes] = None,
              headers: Optional[Mapping[str, str]] = None,
              auth: bool = True) -> Any:
        url = self.base_url + path
        hdrs = {"User-Agent": USER_AGENT, "Accept": "application/json"}
        if auth:
            token = self.token or resolve_token()
            hdrs["Authorization"] = "Bearer " + token
        if headers:
            hdrs.update(headers)
        req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
        try:
            return urllib.request.urlopen(req, timeout=self.timeout)
        except urllib.error.HTTPError as exc:
            raise _error_from_response(exc.code, exc.read()) from exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            raise NetworkError(f"连不上 {self.base_url}：{reason}") from exc

    def _json(self, method: str, path: str, *, data: Optional[bytes] = None,
              headers: Optional[Mapping[str, str]] = None,
              auth: bool = True) -> Any:
        response = self._open(method, path, data=data, headers=headers, auth=auth)
        try:
            raw = response.read()
        finally:
            response.close()
        payload = _parse_json(raw)
        if payload is None:
            raise APIError(f"服务端返回的不是 JSON：{raw[:200]!r}")
        return payload

    def _post_json(self, path: str, payload: Mapping[str, Any], *,
                   auth: bool = True) -> Any:
        body = json.dumps(payload).encode("utf-8")
        return self._json("POST", path, data=body, auth=auth,
                          headers={"Content-Type": "application/json"})

    # -- 账户 -------------------------------------------------------------

    def me(self) -> dict:
        """我是谁 + 还有多少积分。"""
        return self._json("GET", "/api/v1/me")

    def pricing(self) -> dict:
        """计价与限制。动手之前先看，别自己编价格。"""
        return self._json("GET", "/api/v1/pricing")

    def revoke(self) -> dict:
        """吊销当前令牌（怀疑泄露时的紧急出口，不需要登录网页）。"""
        return self._post_json("/api/auth/token/revoke", {})

    # -- 转换 -------------------------------------------------------------

    def convert(self, source: os.PathLike | str, *, ratio: str = "auto",
                confirm_pages: Optional[int] = None) -> ConvertResult:
        """上传一张图或 PDF，立即返回作业号（转换在服务端异步进行）。

        `ratio`：`auto`（跟随原图）/ `16:9` / `4:3`。
        多页 PDF 如果没给 `confirm_pages`，会抛 `PDFConfirmationRequired`，
        让调用方先把页数和扣分告诉用户。
        """
        path = Path(source).expanduser()
        if not path.is_file():
            raise Image2PPTError(f"找不到文件：{path}")
        if ratio not in {"auto", "16:9", "4:3"}:
            raise Image2PPTError(f"ratio 只能是 auto / 16:9 / 4:3，收到 {ratio!r}")

        fields = {"ratio": ratio}
        if confirm_pages is not None:
            fields["confirm_pages"] = str(int(confirm_pages))

        body, ctype = _multipart(fields, path)
        payload = self._json("POST", "/api/v1/convert", data=body,
                             headers={"Content-Type": ctype})
        if payload.get("need_confirm"):
            raise PDFConfirmationRequired(payload)
        if not payload.get("ok", True) or not payload.get("job_id"):
            raise APIError(str(payload.get("msg") or "提交失败"), payload=payload)
        return ConvertResult(
            job_id=str(payload["job_id"]),
            credits=int(payload.get("credits") or 0),
            pages=int(payload.get("pages") or 1),
            total_pages=int(payload.get("total_pages") or 1),
            payload=payload,
        )

    def job(self, job_id: str) -> Job:
        """查一次作业状态。"""
        payload = self._json("GET", f"/api/v1/jobs/{job_id}")
        return Job.from_payload(payload.get("job") or {})

    def wait(self, job_id: str, *, poll_interval: Optional[float] = None,
             timeout: float = DEFAULT_WAIT_TIMEOUT,
             on_progress: Optional[ProgressCallback] = None) -> Job:
        """一直等到作业完成。

        - 网络抖动、429、5xx 只当这一轮没问到，继续等；
        - 401 / 402 / 403 立刻抛，因为重试一万次也不会变；
        - 超时抛 `ConversionTimeout` —— 作业还在跑，**不要重新提交**。
        """
        interval = float(poll_interval if poll_interval is not None else self.poll_interval)
        interval = max(0.0, interval)
        started = time.monotonic()
        last: Optional[Job] = None

        while True:
            elapsed = time.monotonic() - started
            try:
                last = self.job(job_id)
            except NetworkError:
                if elapsed >= timeout:
                    raise ConversionTimeout(
                        f"等了 {int(elapsed)} 秒仍连不上服务端（作业 #{job_id} 可能还在跑）",
                        job_id=job_id, job=last)
                time.sleep(interval)
                continue
            except APIError as exc:
                transient = exc.status_code in {429, 500, 502, 503, 504}
                if transient and elapsed < timeout:
                    time.sleep(interval)
                    continue
                raise

            if on_progress:
                on_progress(last, elapsed)
            if last.failed:
                raise JobFailed(
                    f"转换失败：{last.error or '服务端没有给出原因'}（积分已自动退回）",
                    last)
            if last.done:
                return last
            if elapsed >= timeout:
                raise ConversionTimeout(
                    f"等了 {int(elapsed)} 秒还没完成。作业 #{job_id} 仍在服务端继续跑，"
                    f"稍后用 `image2ppt status {job_id}` 查看，不要重新提交（会再扣一次分）。",
                    job_id=job_id, job=last)

            sleep_for = interval
            remaining = timeout - elapsed
            if remaining > 0:
                sleep_for = min(sleep_for, remaining)
            time.sleep(max(0.0, sleep_for))

    def download(self, job_id: str, output: Optional[os.PathLike | str] = None) -> Path:
        """下载成品 PPTX。`output` 可以是文件路径或已存在的目录。"""
        response = self._open("GET", f"/api/v1/jobs/{job_id}/pptx")
        try:
            name = _filename_from_headers(dict(response.headers.items())) or "result.pptx"
            out = _resolve_output(output, name)
            return _stream_to_file(response, out)
        finally:
            response.close()

    def preview(self, job_id: str, output: Optional[os.PathLike | str] = None) -> Path:
        """下载最新一版中间稿的渲染图（等不及想先看看效果时用）。"""
        response = self._open("GET", f"/api/v1/jobs/{job_id}/preview")
        try:
            name = (_filename_from_headers(dict(response.headers.items()))
                    or f"preview-{job_id}.png")
            out = _resolve_output(output, name)
            return _stream_to_file(response, out)
        finally:
            response.close()

    def download_round(self, job_id: str, round_number: int,
                       output: Optional[os.PathLike | str] = None) -> Path:
        """下载第 n 版中间稿的 PPT（不是每一版都有，`job.round` 那一版一定有）。"""
        response = self._open(
            "GET", f"/api/v1/jobs/{job_id}/round/{int(round_number)}/pptx")
        try:
            name = (_filename_from_headers(dict(response.headers.items()))
                    or f"round-{round_number}.pptx")
            out = _resolve_output(output, name)
            return _stream_to_file(response, out)
        finally:
            response.close()

    def convert_and_wait(self, source: os.PathLike | str, *,
                         output: Optional[os.PathLike | str] = None,
                         ratio: str = "auto",
                         confirm_pages: Optional[int] = None,
                         poll_interval: Optional[float] = None,
                         timeout: float = DEFAULT_WAIT_TIMEOUT,
                         on_progress: Optional[ProgressCallback] = None) -> Conversion:
        """上传 → 等完成 → 下载，一条龙。

        默认产物落在原图旁边：`figure.png` → `figure_可编辑.pptx`。
        """
        started = time.monotonic()
        result = self.convert(source, ratio=ratio, confirm_pages=confirm_pages)
        job = self.wait(result.job_id, poll_interval=poll_interval,
                        timeout=timeout, on_progress=on_progress)
        if output is None:
            src = Path(source).expanduser()
            output = src.with_name(src.stem + "_可编辑.pptx")
        path = self.download(job.id, output)
        return Conversion(path=path, job=job, elapsed=time.monotonic() - started)


def mint_token(email: str, password: str, *, name: str = "",
               base_url: Optional[str] = None,
               timeout: float = DEFAULT_TIMEOUT) -> str:
    """用站内邮箱 + 登录密码换一枚长期令牌（只显示这一次）。

    这是第三方工具**唯一**的入场方式。没设过登录密码的用户要先去站里设一个。
    密码只用于这一次请求，不会被保存。
    """
    if not email or not password:
        raise Image2PPTError("邮箱和密码都要填")
    client = Client(token="", base_url=base_url, timeout=timeout)
    payload = client._post_json(  # noqa: SLF001 - 同一模块内的内部调用
        "/api/auth/token",
        {"email": email.strip().lower(), "password": password, "name": name or ""},
        auth=False,
    )
    token = str(payload.get("token") or "")
    if not token:
        raise APIError("服务端没有返回令牌", payload=payload)
    return token
