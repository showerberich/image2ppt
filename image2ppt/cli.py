"""`image2ppt` 命令行：把图片 / PDF 转成可编辑 PPTX。

设计目标只有一个：**在终端里也能把 20–40 分钟的等待变得可理解**。
退出码固定，方便脚本判断：

    0 成功 · 2 令牌无效 · 3 积分不够 · 4 转换失败 · 5 等待超时 · 6 用法/网络/其他
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import stat
import sys
import time
from pathlib import Path
from typing import Any, Optional

from .client import (
    DEFAULT_WAIT_TIMEOUT,
    APIError,
    AuthenticationError,
    Client,
    ConversionTimeout,
    Image2PPTError,
    InsufficientCredits,
    JobFailed,
    PDFConfirmationRequired,
    mint_token,
)

EXIT_OK = 0
EXIT_AUTH = 2
EXIT_MONEY = 3
EXIT_FAILED = 4
EXIT_TIMEOUT = 5
EXIT_USAGE = 6


def _err(msg: str) -> None:
    print(msg, file=sys.stderr)


def _fmt(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds >= 3600:
        return f"{seconds // 3600} 小时 {seconds % 3600 // 60} 分"
    if seconds >= 60:
        return f"{seconds // 60} 分 {seconds % 60} 秒"
    return f"{seconds} 秒"


def _default_output(source: str | os.PathLike) -> Path:
    src = Path(source).expanduser()
    return src.with_name(src.stem + "_可编辑.pptx")


# ---------------------------------------------------------------------------
# 子命令
# ---------------------------------------------------------------------------

def cmd_me(args: argparse.Namespace, client: Client) -> int:
    data = client.me()
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return EXIT_OK
    print(f"{data.get('email', '')} · 剩余 {data.get('credits', 0)} 积分")
    if int(data.get("credits") or 0) <= 0:
        print(f"⚠️ 没积分了，转换会被拒。充值：{data.get('recharge_url')}")
    return EXIT_OK


def cmd_pricing(args: argparse.Namespace, client: Client) -> int:
    data = client.pricing()
    print(json.dumps(data, ensure_ascii=False, indent=2))
    return EXIT_OK


def cmd_status(args: argparse.Namespace, client: Client) -> int:
    job = client.job(args.job_id)
    if args.json:
        print(json.dumps(job.payload, ensure_ascii=False, indent=2))
        return EXIT_OK
    print(f"#{job.id} {job.filename} · {job.status}"
          + (f" · {job.stage}" if job.stage else ""))
    if job.error:
        print(f"  错误：{job.error}")
    if job.preview:
        print(f"  中间稿：第 {job.preview} 版渲染图"
              + (f"，可下载的是第 {job.round} 版" if job.round else ""))
    if job.has_file:
        print("  成品已就绪，可以下载："
              f"image2ppt download {job.id} -o 结果.pptx")
    return EXIT_OK


def cmd_download(args: argparse.Namespace, client: Client) -> int:
    path = client.download(args.job_id, args.output)
    if args.json:
        print(json.dumps({"ok": True, "path": str(path.resolve())},
                         ensure_ascii=False))
    else:
        print(f"已保存：{path.resolve()}")
    return EXIT_OK


def cmd_token(args: argparse.Namespace, client: Client) -> int:
    """邮箱 + 密码换令牌。密码走 getpass，不留在 shell 历史和进程参数里。"""
    email = (args.email or "").strip()
    if not email:
        _err("请用 --email 指定站内注册邮箱")
        return EXIT_USAGE
    if not sys.stdin.isatty():
        _err("需要在交互式终端里输入密码（不会回显）。")
        return EXIT_USAGE
    password = getpass.getpass("站内登录密码（输入不回显）：")
    if not password:
        _err("密码不能为空")
        return EXIT_USAGE
    token = mint_token(email, password, name=args.name, base_url=args.base)
    token = token.strip()

    if args.save is not None:
        from .client import default_token_file
        path = Path(args.save).expanduser() if args.save else default_token_file()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(token + "\n", encoding="utf-8")
        try:
            path.chmod(stat.S_IRUSR | stat.S_IWUSR)   # 600
        except OSError:
            pass
        _err(f"令牌已保存到 {path}（权限 600）")

    if args.json:
        print(json.dumps({"ok": True, "token": token, "email": email},
                         ensure_ascii=False))
    else:
        print(token)
        _err("这枚令牌只显示这一次，请立刻存好（环境变量 IMAGEPPT_TOKEN）。")
    return EXIT_OK


def cmd_revoke(args: argparse.Namespace, client: Client) -> int:
    if not args.yes:
        if sys.stdin.isatty():
            ans = input("真的吊销当前令牌？吊销后这个工具就不能再调用了 [y/N] ").strip()
            if ans.lower() not in {"y", "yes"}:
                _err("已取消")
                return EXIT_OK
        else:
            _err("非交互环境下请加 --yes 确认吊销")
            return EXIT_USAGE
    data = client.revoke()
    print(json.dumps(data, ensure_ascii=False) if args.json
          else data.get("msg", "令牌已吊销"))
    return EXIT_OK


def _confirm_pdf(exc: PDFConfirmationRequired, args: argparse.Namespace) -> Optional[int]:
    """多页 PDF 的确认：先把页数和扣分讲清楚，再决定转几页。"""
    if args.pages:
        return int(args.pages)
    if args.yes:
        n = exc.pages_to_convert or exc.total_pages or 1
        _err(f"{exc} → --yes 直接按 {n} 页继续")
        return n
    if sys.stdin.isatty():
        raw = input(f"{exc}\n要转前几页？（回车 = 推荐 {exc.pages_to_convert} 页）").strip()
        if not raw:
            return exc.pages_to_convert or 1
        if raw.isdigit() and int(raw) > 0:
            return int(raw)
        _err("页数要填正整数")
        return None
    _err(f"{exc}\n非交互环境下请显式给 --pages N（要转几页）或 --yes（接受推荐值）。")
    return None


def cmd_convert(args: argparse.Namespace, client: Client) -> int:
    source = Path(args.source).expanduser()
    if not source.is_file():
        _err(f"找不到文件：{source}")
        return EXIT_USAGE

    try:
        submitted = client.convert(source, ratio=args.ratio,
                                   confirm_pages=args.pages)
    except PDFConfirmationRequired as exc:
        pages = _confirm_pdf(exc, args)
        if pages is None:
            return EXIT_USAGE
        submitted = client.convert(source, ratio=args.ratio,
                                   confirm_pages=pages)

    cost = submitted.pages if submitted.total_pages > 1 else 1
    if args.json:
        print(json.dumps({"ok": True, "job_id": submitted.job_id,
                          "credits": submitted.credits, "cost": cost},
                         ensure_ascii=False))
    else:
        print(f"作业 #{submitted.job_id} 已提交（本次扣 {cost} 积分，"
              f"余额 {submitted.credits}）")

    if args.no_wait:
        if not args.json:
            print(f"不等了。之后用 `image2ppt status {submitted.job_id}` 查看，"
                  f"完成后再 `image2ppt download {submitted.job_id}`。")
        return EXIT_OK

    last_stage = {"value": ""}

    def on_progress(job: Any, elapsed: float) -> None:
        stage = job.stage or job.status
        if stage and stage != last_stage["value"]:
            last_stage["value"] = stage
            extra = f"（第 {job.preview} 版渲染图）" if job.preview else ""
            print(f"  [{_fmt(elapsed)}] {stage}{extra}", file=sys.stderr, flush=True)

    started = time.monotonic()
    job = client.wait(submitted.job_id, poll_interval=args.poll,
                      timeout=args.timeout, on_progress=on_progress)
    output = args.output or _default_output(source)
    path = client.download(job.id, output)
    elapsed = time.monotonic() - started

    if args.json:
        print(json.dumps({"ok": True, "path": str(path.resolve()),
                          "job_id": job.id, "rounds": job.round,
                          "elapsed_seconds": round(elapsed, 1)},
                         ensure_ascii=False))
    else:
        version = f" · 第 {job.round} 版" if job.round else ""
        print(f"成品已保存：{path.resolve()}（{_fmt(elapsed)}{version}）")
    return EXIT_OK


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="image2ppt",
        description="把图片 / PDF 转成完全可编辑的 PPTX（调用 image2ppt.asia 的 HTTP API）",
    )
    p.add_argument("--base", default=None,
                   help="API 地址（默认 https://image2ppt.asia）")
    p.add_argument("--token", default=None,
                   help="API 令牌；也可以设置环境变量 IMAGEPPT_TOKEN")
    p.add_argument("--json", action="store_true", help="输出 JSON（便于脚本处理）")

    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("me", help="看账号与剩余积分").set_defaults(fn=cmd_me)

    sub.add_parser("pricing", help="看计价与限制").set_defaults(fn=cmd_pricing)

    c = sub.add_parser("convert", help="提交转换并等待成品")
    c.add_argument("source", help="图片或 PDF 路径")
    c.add_argument("-o", "--output", default=None,
                   help="成品保存位置（默认与原图同目录，文件名加「_可编辑」）")
    c.add_argument("--ratio", default="auto", choices=["auto", "16:9", "4:3"],
                   help="页面比例，默认跟随原图")
    c.add_argument("--pages", type=int, default=None,
                   help="多页 PDF 只转前 N 页（按页扣积分）")
    c.add_argument("--yes", action="store_true",
                   help="多页 PDF 不问，直接按服务端推荐页数转")
    c.add_argument("--no-wait", action="store_true",
                   help="提交后立刻返回，稍后用 status / download 取")
    c.add_argument("--poll", type=float, default=20.0,
                   help="轮询间隔秒数（默认 20）")
    c.add_argument("--timeout", type=float, default=DEFAULT_WAIT_TIMEOUT,
                   help=f"最多等多少秒（默认 {int(DEFAULT_WAIT_TIMEOUT)}）")
    c.set_defaults(fn=cmd_convert)

    s = sub.add_parser("status", help="查一个作业")
    s.add_argument("job_id")
    s.set_defaults(fn=cmd_status)

    d = sub.add_parser("download", help="下载已完成的成品")
    d.add_argument("job_id")
    d.add_argument("-o", "--output", default=None,
                   help="保存位置（默认用服务端给的文件名）")
    d.set_defaults(fn=cmd_download)

    t = sub.add_parser("token", help="用邮箱 + 密码换一枚 API 令牌")
    t.add_argument("--email", required=True, help="站内注册邮箱")
    t.add_argument("--name", default="", help="令牌备注，如 claude-code")
    t.add_argument("--save", nargs="?", const="", default=None, metavar="PATH",
                   help="保存到文件（默认 ~/.config/image2ppt/token，权限 600）")
    t.set_defaults(fn=cmd_token)

    r = sub.add_parser("revoke", help="吊销当前令牌")
    r.add_argument("--yes", action="store_true", help="跳过确认")
    r.set_defaults(fn=cmd_revoke)

    return p


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    client = Client(token=args.token, base_url=args.base)

    try:
        return int(args.fn(args, client))
    except AuthenticationError as exc:
        msg = str(exc)
        if "缺少 API 令牌" in msg:
            _err(msg)
        else:
            _err(f"令牌无效或已吊销：{msg}\n"
                 f"到 {client.base_url}/ 重新签发一枚，或检查 IMAGEPPT_TOKEN。")
        return EXIT_AUTH
    except InsufficientCredits as exc:
        _err(f"积分不够：{exc}\n"
             f"请到 {exc.recharge_url} 充值（微信/支付宝），充完直接重试，不用换令牌。")
        return EXIT_MONEY
    except JobFailed as exc:
        _err(str(exc))
        return EXIT_FAILED
    except ConversionTimeout as exc:
        _err(str(exc))
        return EXIT_TIMEOUT
    except APIError as exc:
        code = f"HTTP {exc.status_code}" if exc.status_code else "API"
        _err(f"请求失败（{code}）：{exc}")
        return EXIT_USAGE
    except (Image2PPTError, OSError) as exc:
        _err(str(exc))
        return EXIT_USAGE
    except KeyboardInterrupt:
        _err("\n已中断。作业在服务端照跑，用 `image2ppt status <job_id>` 还能查到它。")
        return EXIT_USAGE


if __name__ == "__main__":
    raise SystemExit(main())
