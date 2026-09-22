#!/usr/bin/env python3
"""把 unittest / cc_parity 的失败输出写成 GitHub Actions 注解。

**为什么需要它。** job 日志要仓库 **admin 权限**才下载得到
（`403 Must have admin rights`），而 `::error::` 注解经 check-runs API
**公开可读**。一条「让没有那个平台的人看见那个平台的问题」的 CI，
失败信息只有仓库主人看得到，等于没有。

**为什么是一个脚本而不是 workflow 里的 heredoc。** 2026-09-21 第一版就是
heredoc，在 Windows 上一个注解都没出来——Git Bash 的 `tee /tmp/unit.log` 写的是
MSYS 的 `/tmp`，而紧接着运行的是 **Windows 的 python**，它把 `"/tmp/unit.log"`
当成盘符相对路径去找 `C:\\tmp\\unit.log`，`FileNotFoundError`。同一个坑
zylab 自己有 `tools._from_shell_path` 专门处理，我在 workflow 里又踩了一遍。
收成脚本 + 相对路径之后，两个平台解析的是同一个字符串。

注解每步只显示 10 条，所以**打包**：一条失败清单、最多两条完整 traceback、
一条输出尾部。一行一个失败名只够看见名字，看不到为什么。
"""
from __future__ import annotations

import re
import sys

MAX_BODY = 3500          # 单条注解的正文上限，留足余量给 %0A 转义
MAX_NAMES = 40
MAX_BLOCKS = 2
TAIL_LINES = 8
# unittest 的一个失败块长这样：
#   ====== (70 个 =)
#   FAIL: test_x (...)
#   ------ (70 个 -)        ← 这条属于**本块的头**，不是结束
#   Traceback ...
# 所以必须显式吃掉头里那条虚线，再停在「下一条 ===== 」或「收尾的 ----- + Ran」。
# 第一版只写 (?=\n={70}|\n-{70})，于是每个块都在头部就被截断——正文一行都没带上，
# 而块数看起来还是对的（2026-09-21，靠 tests/test_ci_annotate.py 抓到）。
BLOCK = re.compile(
    r"={70}\n(?:FAIL|ERROR):[^\n]*\n-{70}\n.*?(?=\n={70}\n|\n-{70}\nRan |\Z)",
    re.S)


def escape(text):
    """GitHub 的 workflow command 要求换行与 % 转义，否则注解会被截在第一行。"""
    return (str(text)[:MAX_BODY]
            .replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A"))


def annotations(log):
    """返回 [(标题, 正文)]，最多 4 条。没有可报的就返回空。"""
    out = []
    names = [line for line in log.splitlines()
             if line.startswith(("FAIL:", "ERROR:"))]
    if names:
        out.append(("失败清单", "\n".join(names[:MAX_NAMES])))
    for index, block in enumerate(BLOCK.findall(log)[:MAX_BLOCKS], 1):
        out.append((f"traceback {index}", block))
    parity = [line for line in log.splitlines() if "❌" in line or "⁉" in line]
    if parity:
        out.append(("记分板回归", "\n".join(parity[:MAX_NAMES])))
    tail = "\n".join(log.splitlines()[-TAIL_LINES:]).strip()
    if tail:
        out.append(("尾部", tail))
    return out


def force_utf8_stdout(stream=None):
    """把 stdout 改成 UTF-8。**不改就一条注解都出不来。**

    Windows 上写**管道**（GitHub 的 bash step 就是管道）时，Python 用的是本地
    代码页——cp1252 / cp936 编不出中文，`print("::error title=失败清单::…")`
    当场 `UnicodeEncodeError`，而 workflow 里那句 `|| true` 把它咽掉，于是
    「CI 红了但一条注解都没有」（2026-09-21 连续两轮都栽在这里）。

    zylab 自己有 `wincompat.configure_stdio()` 干同一件事，但这个脚本刻意不
    import core——它要在测试套件跑挂了之后还能工作，不该依赖被测的那棵树。
    """
    target = sys.stdout if stream is None else stream
    reconfigure = getattr(target, "reconfigure", None)
    if reconfigure is None:                       # 被替换成了别的对象
        return target
    try:
        reconfigure(encoding="utf-8", errors="replace")
    except (OSError, ValueError):                 # 不支持就算了，下面还有兜底
        pass
    return target


def main(argv):
    force_utf8_stdout()
    if len(argv) != 2:
        print("用法: ci_annotate.py <日志文件>", file=sys.stderr)
        return 2
    try:
        with open(argv[1], encoding="utf-8", errors="replace") as handle:
            log = handle.read()
    except OSError as exc:
        # 报告器自己坏掉，不该盖住被报告的那个失败。
        print(f"::warning::读不到 {argv[1]}：{exc}")
        return 0
    for title, body in annotations(log):
        line = f"::error title={title}::{escape(body)}"
        try:
            print(line)
        except UnicodeEncodeError:
            # 兜底：编不出就退成 ASCII。少几个汉字也比一条注解都没有好。
            print(line.encode("ascii", "backslashreplace").decode("ascii"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
