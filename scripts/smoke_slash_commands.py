#!/usr/bin/env python3
"""把每个斜杠命令在隔离 HOME 的真实 REPL（PTY）里各调一次，报告崩溃/错误/卡死。

为什么存在：单元测试覆盖的是各命令的逻辑分支，没有一处"每个入口都真按一遍"。
2026-09-04 用户反馈 /graft 不顺，顺手把全部入口过一遍。每个命令一个 PTY 会话（互不污染），
发送命令 → 两次 Esc（关掉可能弹出的选择器/输入框）→ /exit；网关指向本机关闭端口，任何
provider 调用都会快速失败而不是等网络。

用法：python3 scripts/smoke_slash_commands.py [--only cmd,cmd] [--show cmd]
"""
import argparse
import os
import re
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
os.environ.setdefault("TERM", "xterm-256color")

from tests.pty_harness import PTYSend, run_pty_child           # noqa: E402
from tests.test_thinking_pty import HEAD                         # noqa: E402

ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07")
# 各命令的冒烟参数：默认空串；需要参数的给一个安全值。exit 单独作为收尾。
ARGS = {
    "expand": "", "kill": "nope", "task": "nope", "rename": "smoke",
    "graft": "doctor", "gateway": "", "probe": "status", "model": "help",
    "workflow": "help", "memory": "", "architecture": "", "consult": "",
    "rewind": "", "resume": "", "permissions": "", "commands": "",
}
SKIP = {"exit"}
ERROR_MARKS = ("Traceback", "Error:", "错误", "失败", "unavailable", "不可用", "[", "无法")

TAIL = r'''
client.stream_chat = fake_stream
session = zylab.Session(agent)
agent.confirm = session.confirm
announce_when(lambda: (calls["n"] >= 0 and not session.pump.snapshot().busy
                       and "空闲" in session.pump.snapshot().activity),
              "ZYLAB_TEST_IDLE")
zylab.repl(session, "m")
print("RESULT:" + json.dumps({"ok": True}))
'''
FAKE = r'''
def fake_stream(model, messages, **kwargs):
    calls["n"] += 1
    yield {"t": "text", "v": "smoke"}
    yield {"t": "done", "reason": "stop", "usage": {}}
'''


def run_one(name, args):
    prefix = ("import os; os.environ['ZYLAB_BASE'] = 'http://127.0.0.1:9/v1'; "
              "os.environ['DEEPINFER_API_KEY'] = 'smoke-key'\n")
    line = f"/{name} {args}".strip()
    started = time.monotonic()
    try:
        out, _ = run_pty_child(
            HEAD + FAKE + TAIL,
            [PTYSend((line + "\r").encode(), after="ZYLAB_TEST_PUMP_READY", delay=0.3),
             PTYSend(b"\x1b", delay=2.0),
             PTYSend(b"\x1b", delay=0.4),
             PTYSend(b"/exit\r", delay=0.6)],
            cwd=ROOT, timeout=30.0, prefix=prefix)
    except Exception as exc:                            # noqa: BLE001
        text = ANSI.sub("", str(exc))
        verdict = "HANG/CRASH" if "超时" in text or "Timeout" in text else "CRASH"
        return verdict, round(time.monotonic() - started, 1), text[-1200:]
    plain = ANSI.sub("", out).replace("\r", "")
    # 只看命令回显之后、/exit 之前的那段
    start = plain.find(line)
    segment = plain[start + len(line):] if start >= 0 else plain
    end = segment.find("/exit")
    if end >= 0:
        segment = segment[:end]
    body = "\n".join(
        l for l in segment.splitlines()
        if l.strip() and "ZYLAB_TEST" not in l and not l.strip().startswith(("╭", "╰", "│ ›")))
    # 只看"提示行"（以 [xxx] 开头的通知）和 Traceback；工具/命令描述里的"失败"二字不算
    notices = [
        l.strip() for l in body.splitlines()
        if re.match(r"^\s*\[[^\]]+\]", l) or l.strip().startswith("Traceback")]
    if any("Traceback" in l for l in notices) or "Traceback" in body:
        verdict = "CRASH"
    elif any(any(mark in l for mark in ("错误", "失败", "无法", "unavailable", "不可用", "Error"))
             for l in notices):
        # 故意传非法参数（kill/task nope）时报错是预期行为，不算问题
        verdict = "ok(expected-error)" if ARGS.get(name) == "nope" else "ERROR?"
    else:
        verdict = "ok"
    return verdict, round(time.monotonic() - started, 1), body[-1200:]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", default="")
    parser.add_argument("--show", default="", help="打印这些命令的完整输出（逗号分隔，或 all）")
    ns = parser.parse_args()
    import zylab                                          # noqa: E402
    names = sorted(zylab.REGISTRY)
    if ns.only:
        names = [n for n in names if n in set(ns.only.split(","))]
    show = set(ns.show.split(",")) if ns.show else set()
    rows = []
    for name in names:
        if name in SKIP:
            continue
        verdict, seconds, body = run_one(name, ARGS.get(name, ""))
        rows.append((name, verdict, seconds, body))
        flag = "" if verdict.startswith("ok") else "  <--"
        print(f"{name:14} {verdict:18} {seconds:5.1f}s{flag}", flush=True)
        if name in show or "all" in show or not verdict.startswith("ok"):
            # 命令自己的输出在段首；段尾永远是 /exit 时弹出的菜单帧
            for l in body.splitlines()[:30]:
                print("    " + l[:150])
    bad = [r for r in rows if not r[1].startswith("ok")]
    print(f"\n{len(rows)} commands · {len(rows) - len(bad)} ok · {len(bad)} to look at")
    return 1 if any(r[1] == "CRASH" or r[1].startswith("HANG") for r in rows) else 0


if __name__ == "__main__":
    sys.exit(main())
