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

注解每步只显示 10 条，所以**打包**：一条根因汇总、一条失败清单、最多 MAX_BLOCKS 条
完整 traceback（每个根因一条）、一条输出尾部。一行一个失败名只够看见名字，
看不到为什么。

**根因汇总是这里最贵的一条。** 2026-09-22 的 Windows 那列带回 40 个失败名 + 2 条
traceback，而那 2 条**是同一个根因**（py3.10 的 `chmod: follow_symlinks`）——
等于 40 条失败只换到 1 份信息，还看不出是 1 个根因还是 40 个。按 traceback 的
**最后一行异常**归并之后，一眼就能定性，选 traceback 时也改成**每个根因只取一条**。
"""
from __future__ import annotations

import re
import sys

MAX_BODY = 3500          # 单条注解的正文上限，留足余量给 %0A 转义
MAX_NAMES = 40
# 每个根因取一条 traceback。3 → 5：头两轮根因多达 51/59 类，注解预算用在
# 「根因汇总」上更值；现在崩溃类清完、根因降到 14/46 类，预算该换成深度了。
# 上限来自 GitHub 每步只显示 ~10 条注解：
# 根因汇总 + 失败清单 + 5 条 traceback + 尾部 = 8，留一条给 GitHub 自己。
MAX_BLOCKS = 5
MAX_CAUSES = 12
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
# faulthandler 的挂起转储。`tests/__init__.py` 在 CI 上武装了
# `dump_traceback_later(..., exit=True)`，到点会打出所有线程的栈。
#
# **它是「最近调用在最前」的**，所以挂在哪儿要看**开头**几十行，而「尾部」那条
# 注解取的是最后 8 行——那是 unittest 最外层的 runner 帧，一点信息都没有。
# 2026-09-22 macOS 那列就吃了这个：看门狗准时开火、栈也打出来了，
# 注解里却只有 `unittest/main.py in runTests`。
HANG = re.compile(r"^Timeout \([^)]*\)!.*", re.M)
HANG_LINES = 26


def escape(text):
    """GitHub 的 workflow command 要求换行与 % 转义，否则注解会被截在第一行。"""
    return (str(text)[:MAX_BODY]
            .replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A"))


# 归并键要把「同一个根因在不同用例上的不同现场」抹平：临时目录、绝对路径、
# 对象地址、长数字每次都不一样，留着它们等于不归并。
_NOISE = re.compile(r"""['"]?(?:[A-Za-z]:[\\/]|/)[^\s'"]+['"]?|0x[0-9a-fA-F]+|\d{3,}""")


def _last_line(block):
    lines = [line for line in block.splitlines() if line.strip()]
    return lines[-1].strip() if lines else ""


# 「根因行」取的是失败块的最后一行，而 PTY 用例的最后一行常常是**一整屏终端
# 转储**（几千字符的转义序列）。它会把「根因汇总」那条注解的正文预算一口吃光，
# 于是后面十几类根因一条都显示不出来 —— 2026-09-22 实测：Windows py3.10 那列
# 48 类根因，摘要里只看得见 2 类。
#
# 所以显示前先压：去掉控制字符、压掉连续空白、截到 CAUSE_LINE 字符。
# **归并键单独算**（cause_key），不受显示截断影响。
CAUSE_LINE = 150
NAME_LINE = 96      # 「首例」只是用例名，不需要 150
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def tidy_cause(line, limit=None):
    """把一行根因压成可显示的一行——纯函数，测得到。"""
    limit = CAUSE_LINE if limit is None else limit
    text = _CONTROL.sub("", str(line)).replace("\x1b", "")
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit] + ("…" if len(text) > limit else "")


def cause_key(line):
    """把一行异常收敛成「根因」——纯函数，测得到。"""
    return _NOISE.sub("…", str(line))[:200]


def exception_class(line):
    """从异常行里取出类名（`pkg.mod.Name: msg` → `Name`）；取不到就返回原样。

    归并的**第二层**。2026-09-22 的 Windows 那列报了 59 类根因，而一条注解只放得下
    十来条 —— 剩下 44 类连形状都看不见。多数尾巴是只出现一次的单例，按异常类再收
    一次，就能用一行说清「这 59 类里有多少是断言失败、多少是 OSError」。
    """
    head = str(line).split(":", 1)[0].strip()
    if not head or " " in head:
        return "（无异常行）"
    return head.rsplit(".", 1)[-1] or head


def class_histogram(causes):
    """[(根因行, 条数, …)] → 「AssertionError×31 · OSError×12」这样的一行。"""
    tally = {}
    for line, count, *_ in causes:
        name = exception_class(line)
        tally[name] = tally.get(name, 0) + count
    ordered = sorted(tally.items(), key=lambda kv: (-kv[1], kv[0]))
    return " · ".join(f"{name}×{count}" for name, count in ordered)


def group_by_cause(blocks):
    """按根因归并失败块，保序返回 [(根因行, 条数, 首个失败名, 首个块)]。

    「首个」而不是「最后」：第一次出现的那条最可能是**触发**它的用例，
    后面的多半是同一个 setUp 连带的。
    """
    order = []
    seen = {}
    for block in blocks:
        head = block.splitlines()[1] if len(block.splitlines()) > 1 else ""
        key = cause_key(_last_line(block))
        if key not in seen:
            seen[key] = {"line": _last_line(block), "count": 0,
                         "name": head.strip(), "block": block}
            order.append(key)
        seen[key]["count"] += 1
    return [(seen[k]["line"], seen[k]["count"], seen[k]["name"], seen[k]["block"])
            for k in order]


def pick_blocks(causes, limit=None):
    """从根因里挑 traceback：**首尾交替**，不是取前 N 个。

    unittest 的执行顺序是按模块名字母序，于是「取前 N 个根因」会系统性地偏向
    字母靠前的模块——2026-09-22 实测：macOS 那列 13 类根因取 5 条，全落在
    test_agent_* / test_app_* / test_client / test_ctrl_o / test_metrics 上，
    而我正想看的 test_sessions 与 test_windows_paths 在字母末尾，**永远轮不到**。

    首尾交替（第 1、最后、第 2、倒数第 2…）让两端都有代表，代价是零。
    """
    limit = MAX_BLOCKS if limit is None else limit
    remaining = list(causes)
    out = []
    while remaining and len(out) < limit:
        out.append(remaining.pop(0))
        if remaining and len(out) < limit:
            out.append(remaining.pop())
    return out


def annotations(log):
    """返回 [(标题, 正文)]；条数受 MAX_BLOCKS 约束，没有可报的就返回空。"""
    out = []
    names = [line for line in log.splitlines()
             if line.startswith(("FAIL:", "ERROR:"))]
    causes = group_by_cause(BLOCK.findall(log))
    if causes:
        head = f"{len(causes)} 类根因 / {sum(c for _, c, _, _ in causes)} 个失败块"
        body = [head, f"按异常类：{class_histogram(causes)}"]
        for line, count, name, _ in causes[:MAX_CAUSES]:
            body.append(f"{count:4d}×  {tidy_cause(line)}")
            body.append(f"       首例 {tidy_cause(name, NAME_LINE)}")
        out.append(("根因汇总", "\n".join(body)))
    if names:
        out.append(("失败清单", "\n".join(names[:MAX_NAMES])))
    for index, (_, _, _, block) in enumerate(pick_blocks(causes), 1):
        out.append((f"traceback {index}", block))
    hit = HANG.search(log)
    if hit:
        head = log[hit.start():].splitlines()[:HANG_LINES]
        out.append(("挂起处（最近调用在最前）", "\n".join(head)))
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
