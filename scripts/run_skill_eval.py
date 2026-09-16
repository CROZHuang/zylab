#!/usr/bin/env python3
"""ES0a：对真实模型跑一遍 skill 触发评测。

**这个脚本会发真实 provider 请求**，所以刻意放在 `scripts/` 而不是 `tests/`：
`python3 -m unittest discover` 不会碰它。判据来自 journal 的 tool_started
事件，不是终端输出 —— 后者会被折叠、换行、着色干扰，也拿不进 CI。

    python3 scripts/run_skill_eval.py                    # 全部用例
    python3 scripts/run_skill_eval.py --case vendor-docs-context-limit
    python3 scripts/run_skill_eval.py --repeat 3         # 同一用例跑多次看稳定性
    python3 scripts/run_skill_eval.py --out before.json  # 存基线，供 A/B 对比
    python3 scripts/run_skill_eval.py --compare before.json

每个用例是一次完整 turn。耗时**重尾**：2026-08-31 同一批 16 个用例实测
5s–929s，中位 74s。默认串行，`--jobs` 可并行；并行只影响墙钟，不影响判据。

`--timeout` 是**软**上限：cancel 只在流式响应内部与每个工具调用之前被检查，
所以实际停止晚于截止（实测 20s 截止停在 173s）。它防的是无限挂起，不是精确
限时。真要硬上限得把每个用例放进子进程再杀，那是更大的改动，当前没做。

**HOME 隔离**：脚本自建临时 HOME，不写真实 `~/.zylab/`（AGENTS.md §3）。
"""
import argparse
import json
import os
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "tests"))

import skill_eval                                             # noqa: E402


def authorize_gateway(gateway):
    """选定网关，必要时对**这一个 endpoint** 做窄授权。

    有些内部网关只提供明文 HTTP。zylab 默认拒绝向明文 provider 发送 key ——
    那是刻意的闸，不是 bug。这里用 `allowed_insecure_endpoints`（精确 endpoint）
    而不是 `allow_insecure_http=True`（进程级大开关）：前者是窄的持久同意，
    后者会把这一整次运行里所有明文 provider 都放行。
    """
    from core import client
    client.set_gateway(gateway)
    base = client.resolve_base(gateway)
    if not base:
        raise SystemExit(client.base_missing_hint(gateway))
    if base.lower().startswith("http://"):
        print(f"  ! {gateway} 走明文 HTTP（{base}）——"
              " 本次运行的 API key 与 prompt 会以明文经过链路。")
        client.configure_transport_policy(allowed_insecure_endpoints=(base,))
    return base


def run_case(case, *, index_text, model=None, timeout=900):
    """跑一个用例；超时按「不可判定」处理，不按「未命中」。

    每个用例用全新 Agent —— 上一个用例读过的 skill 不能影响下一个（PLAN 的
    触发规则本身就写了「不跨轮沿用」，评测更不能跨用例沿用）。

    **HOME 不在这里改。** 第一版在这里 setenv + reload(store)，两个 bug：
    临时 HOME 里没有 `.zylab` 目录（StateError），且 `--jobs>1` 时多个线程
    改同一个全局环境变量 —— 竞态。现在 HOME 在 import core 之前就设好（见
    main()），每个模块 import 时自然算出临时路径，不需要任何 reload。
    """
    from core import agent as A
    started = time.time()
    ag = A.Agent(model=model or A.MODEL, skills_context=index_text)
    captured = []
    # 耗时是重尾的：2026-08-31 同一批用例 5s 到 929s，差 180 倍。没有上界时
    # 一条卡住的用例会拖着整轮不结束，而它贡献的信息为零。
    cancel = threading.Event()
    timer = threading.Timer(timeout, cancel.set)
    timer.daemon = True
    timer.start()
    try:
        for _ in ag.run(case["prompt"], cancel=cancel,
                        event_sink=lambda evs: (captured.extend(evs) or evs)):
            pass
    except Exception as exc:                                  # noqa: BLE001
        graded = skill_eval.grade(case, skill_eval.skills_read(captured),
                                  completed=False)
        graded["error"] = f"{type(exc).__name__}: {exc}"
        return graded, time.time() - started, graded["error"]
    finally:
        timer.cancel()
    read = skill_eval.skills_read(captured)
    timed_out = cancel.is_set()
    failed = skill_eval.turn_failed(captured) or timed_out
    graded = skill_eval.grade(case, read, completed=not failed)
    if timed_out:
        # 超时**不是**未命中。已经读到的 skill 仍然算数（观察到的事实），
        # 只有「没读到」因为可能没轮到而不可判定。
        graded["error"] = f"timeout after {timeout}s"
        return graded, time.time() - started, graded["error"]
    if failed:
        # 网关 503／超时／被打断都走这里。注意这**不**一律作废 —— 已经读到的
        # skill 是观察到的事实，仍算数；只有"没读到"才因可能没轮到而不可判定。
        # 原因要显示出来：光说 turn_failed 会让人以为是 harness 坏了。
        graded["error"] = f"turn_failed: {_failure_reason(captured)}"
    return graded, time.time() - started, graded["error"]


def _failure_reason(events):
    for row in reversed(events or ()):
        if (row.get("kind") if isinstance(row, dict) else None) != "turn_failed":
            continue
        payload = row.get("payload") or {}
        kind = payload.get("error_type") or "unknown"
        detail = str(payload.get("error") or "")[:90]
        return f"{kind} {detail}" if detail else kind
    return "unknown"


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--case", action="append", help="只跑指定用例名，可重复")
    ap.add_argument("--repeat", type=int, default=1, help="每个用例跑几次")
    ap.add_argument("--jobs", type=int, default=1, help="并行度")
    ap.add_argument("--model", default=None)
    ap.add_argument("--timeout", type=int, default=900,
                    help="单个用例的墙钟上限（秒），超时按不可判定处理")
    ap.add_argument("--gateway", default=None,
                    help="deepinfer|boyue；默认用 client 的当前网关")
    ap.add_argument("--out", help="把结果写成 JSON 基线")
    ap.add_argument("--compare", help="与一份基线对比并只报告差异")
    args = ap.parse_args()

    # HOME 必须在 import core 之前设好：core 的多个模块在 import 时就从 HOME
    # 派生路径（store.HOME、commands.USER_ROOT、skills.USER_ROOT…），
    # 事后 reload 只能改到其中一部分。
    home = tempfile.mkdtemp(prefix="zylab-skill-eval-")
    os.makedirs(os.path.join(home, ".zylab"), mode=0o700, exist_ok=True)
    os.environ["HOME"] = home
    print(f"  隔离 HOME: {home}", flush=True)

    if args.gateway:
        base = authorize_gateway(args.gateway)
        print(f"  网关: {args.gateway} · {base}", flush=True)

    from core import skills
    index_text = skills.render_index(skills.load(cwd=str(REPO)))["text"]

    cases = skill_eval.load_cases()
    if args.case:
        wanted = set(args.case)
        cases = [c for c in cases if c["name"] in wanted]
        missing = wanted - {c["name"] for c in cases}
        if missing:
            sys.exit(f"没有这些用例：{sorted(missing)}")
    plan = [c for c in cases for _ in range(max(1, args.repeat))]
    print(f"  {len(plan)} 次运行（{len(cases)} 用例 × {args.repeat}）· "
          f"并行 {args.jobs} · 每次约 60–120s", flush=True)

    def report(case, graded, secs, error):
        """完成一个报一个。

        第一版是全跑完再打印 —— 14 个用例 × 约 90 秒，并行 3 也要十分钟黑屏，
        重定向到文件时还叠加块缓冲，看上去像卡死了。flush 是必须的。
        """
        # 标记优先级 = 信号强度。**已观察到的事实压过不可判定** ——
        # 2026-08-31 实测里 test-the-composer-redraw 读到了正确的 skill 却因
        # turn 后续失败被标成 `?`，把一次真实的命中显示成了「不知道」。
        if not graded["negative_ok"]:
            mark = "!"                    # 过度触发：观察结果，永远算数
        elif graded["positive_ok"] is True and graded.get("positive_valid"):
            mark = "✓"                    # 命中：观察结果，中途失败也算数
        elif (graded["positive_ok"] is not None
              and not graded.get("positive_valid")) \
                or not graded.get("negative_valid"):
            mark = "?"                    # 没跑完，且没观察到可用信号
        elif graded["positive_ok"] is False:
            mark = "✗"
        else:
            mark = "·"
        elapsed.append(secs)
        got = ",".join(graded["read"]) or "—"
        print(f"  {mark} {case['name']:32} {secs:5.0f}s  读到: {got}"
              + (f"  [{error}]" if error else ""), flush=True)
        if graded["false_positive"]:
            print(f"      过度触发: {','.join(graded['false_positive'])}",
                  flush=True)
        return graded

    rows, elapsed = [], []
    wall_started = time.time()
    if args.jobs > 1:
        with ThreadPoolExecutor(max_workers=args.jobs) as pool:
            futures = {pool.submit(run_case, c, index_text=index_text,
                                   model=args.model): c for c in plan}
            for future in as_completed(futures):
                rows.append(report(futures[future], *future.result()))
    else:
        for case in plan:
            rows.append(report(case, *run_case(
                case, index_text=index_text, model=args.model,
                timeout=args.timeout)))
    summary = skill_eval.summarize(rows)
    print(f"\n  正向 {summary['positive_ok']}/{summary['positive_total']} · "
          f"负向 {summary['negative_ok']}/{summary['negative_total']}"
          + (f" · 不可判定 {summary['indeterminate']}"
             if summary["indeterminate"] else "")
          + (f" · turn 失败 {summary['errored']}" if summary["errored"] else ""))
    if summary["indeterminate"]:
        print("  ! 有轴不可判定：这一轮没跑完，且没观察到可用信号。"
              "重跑它们再看结论 —— 没跑完不等于没触发。")

    if elapsed:
        # 耗时是重尾的，均值会骗人 —— 报中位与最长，并点名最慢那条。
        ordered = sorted(elapsed)
        median = ordered[len(ordered) // 2]
        slowest = max(range(len(elapsed)), key=elapsed.__getitem__)
        print(f"  墙钟 {time.time() - wall_started:.0f}s · 单例中位 {median:.0f}s"
              f" · 最长 {ordered[-1]:.0f}s（{rows[slowest]['case']}）"
              f" · 上限 {args.timeout}s")
        over = [r["case"] for r in rows
                if str(r.get("error") or "").startswith("timeout")]
        if over:
            print(f"  ! 超时未跑完：{', '.join(over)} —— 这些**不是**未命中，"
                  "是不可判定。放宽 --timeout 或单独重跑。")

    if args.out:
        Path(args.out).write_text(
            json.dumps({"summary": summary, "results": rows},
                       ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        print(f"  基线已写入 {args.out}")

    if args.compare:
        old = json.loads(Path(args.compare).read_text(encoding="utf-8"))
        before = {r["case"]: r for r in old["results"]}
        changed = 0
        for row in rows:
            was = before.get(row["case"])
            if was is None:
                print(f"  + 新用例 {row['case']}")
                changed += 1
                continue
            if was["read"] != row["read"]:
                print(f"  ~ {row['case']}: {was['read'] or '—'} → "
                      f"{row['read'] or '—'}")
                changed += 1
        print(f"  与基线相比 {changed} 处变化" if changed else "  与基线一致")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
