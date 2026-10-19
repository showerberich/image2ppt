#!/usr/bin/env python3
"""最小可运行示例：把一张图转成可编辑 PPTX。

用法：

    export IMAGEPPT_TOKEN=ppt_xxx
    python examples/convert_example.py figure.png [result.pptx]

令牌也可以放在 ~/.config/image2ppt/token（先用
`image2ppt token --email 你的邮箱 --save` 生成）。
"""

from __future__ import annotations

import sys
from pathlib import Path

from image2ppt import Client, ConversionTimeout, InsufficientCredits, JobFailed


def main(argv: list[str]) -> int:
    if len(argv) not in {2, 3}:
        print(__doc__.strip(), file=sys.stderr)
        return 2

    source = Path(argv[1]).expanduser()
    output = Path(argv[2]).expanduser() if len(argv) == 3 else None

    with Client() as client:
        me = client.me()
        print(f"账号：{me.get('email')} · 剩余 {me.get('credits')} 积分")

        try:
            result = client.convert_and_wait(
                source,
                output=output,
                on_progress=lambda job, seconds: print(
                    f"  [{seconds:6.0f}s] {job.stage or job.status}"
                    + (f"（第 {job.preview} 版）" if job.preview else "")
                ),
            )
        except InsufficientCredits as exc:
            print(f"积分不够，请到 {exc.recharge_url} 充值后重试。", file=sys.stderr)
            return 3
        except JobFailed as exc:
            print(f"转换失败：{exc.job.error or exc}", file=sys.stderr)
            return 4
        except ConversionTimeout as exc:
            print(f"还没跑完：{exc}\n用 `image2ppt status {exc.job_id}` 查看，"
                  "不要重新提交。", file=sys.stderr)
            return 5

    print(f"成品：{result.path}（{result.elapsed:.0f} 秒 · 第 {result.job.round} 版）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
