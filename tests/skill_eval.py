"""Skill 触发评测 harness（ES0a）。

**判据必须来自真实的 read 事件**，不是模型自报，也不是 grep 终端输出。

2026-08-31 的手工 A/B 是靠在终端输出里数 `⚑ skill:` 标记做的 —— 那个信号
会被折叠、换行、着色干扰，而且拿不进 CI。这里改从 journal 的 `tool_started`
事件读：模型是否真的读了某条 SKILL.md，是一个布尔事实，不需要人裁量。
（PLAN §ES6 退出条件：「使用状态来源于真实 read 事件，而不是模型自报」。）

计划要求把三件事**分开判定**，不能混为一谈：

    index_visible   索引里有没有这条            —— 静态，不需要跑模型
    body_read       正文有没有被真的读取        —— 本模块，读 journal
    behavior        流程有没有产生预期证据      —— ES0b，不在本轮范围

两条主循环的 tool_started payload 形状不同，这里抹平：

    run()          {"tool_call_id", "name", "args": {...}}
    _run_managed   {"tool_call": {...OpenAI 形状的 tool_call...}}

本模块**不是**测试文件（`discover -p 'test_*.py'` 不会收集它），也不发起任何
provider 请求。真机评测在 `scripts/run_skill_eval.py`，需要显式运行。
"""
import json
import os
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
CASES = Path(__file__).resolve().parent / "fixtures" / "skill_eval" / "cases.json"

SKILL_FILE = "SKILL.md"


def _tool_call_fields(payload):
    """把两种 payload 形状归一成 (name, args_dict)。

    形状差异来自 run() 与 _run_managed 是两条独立主循环 —— 这是 DEVLOG 里
    记着的尾巴，harness 必须同时认两种，否则只能测其中一条路径。
    """
    if not isinstance(payload, dict):
        return None, {}
    if "name" in payload:                       # run()
        args = payload.get("args")
        return payload.get("name"), args if isinstance(args, dict) else {}
    call = payload.get("tool_call")              # _run_managed
    if not isinstance(call, dict):
        return None, {}
    fn = call.get("function") or {}
    raw = fn.get("arguments")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except (ValueError, TypeError):
            raw = {}
    return fn.get("name"), raw if isinstance(raw, dict) else {}


def skill_name_from_path(path):
    """`.../skills/<name>/SKILL.md` → `<name>`；不是 skill 文件则 None。

    与 zylab.py 的 `_skill_mark` 判据保持一致：父目录名就是 skill 名，
    没有父目录就说不出是哪条，宁可不认。
    """
    parts = str(path or "").replace("\\", "/").rstrip("/").split("/")
    if len(parts) < 2 or parts[-1] != SKILL_FILE or not parts[-2]:
        return ""
    return parts[-2]


def skills_read(events):
    """从 journal 事件里取出被读取的 skill 名，保持首次出现的顺序。"""
    seen, out = set(), []
    for row in events or ():
        if (row.get("kind") if isinstance(row, dict) else None) != "tool_started":
            continue
        name, args = _tool_call_fields(row.get("payload"))
        if name != "read_file":
            continue
        skill = skill_name_from_path(args.get("path"))
        if skill and skill not in seen:
            seen.add(skill)
            out.append(skill)
    return out


def index_names(index_text):
    """索引文本里出现了哪些 skill 名 —— 静态判据，不需要跑模型。"""
    return [line.split(" | ", 1)[0].strip()
            for line in str(index_text or "").splitlines() if line.strip()]


def load_cases(path=CASES):
    with open(path, encoding="utf-8") as handle:
        data = json.load(handle)
    return data["cases"]


def grade(case, read, *, completed=True):
    """一个用例的判定。**正向与负向分开报告**，不合成单一分数。

    合成分数会掩盖最需要看见的一类失败：该触发的没触发、不该触发的触发了，
    在总分上可能互相抵消。

    `completed` 表示这一轮有没有跑完（没撞上 503／超时／中断）。判定的原则是
    docs/pipeline-lessons.md 的 F1「断言不存在」——

        **观察到的事件永远有效；"没观察到"只有在这一轮跑完时才有意义。**

    所以 turn 中途失败时：已经读到的 skill 仍然算数（那是事实），但"它没读
    某条"不算数（可能只是没轮到）。两条轴各自标 valid，不合并成一个作废标记。
    2026-08-31 第一次真机冒烟就撞上这个：模型读了 vendor-docs 之后网关 503，
    粗判据把这条**已观察到的命中**一并作废了。
    """
    expect_any = list(case.get("expect_any") or ())
    expect_none = list(case.get("expect_none") or ())
    got = set(read)
    hit = [s for s in expect_any if s in got]
    false_positive = [s for s in expect_none if s in got]
    return {
        "case": case["name"],
        "error": None,
        "completed": bool(completed),
        "read": list(read),
        "positive_ok": bool(hit) if expect_any else None,
        # 命中是观察结果，中途失败也算数；未命中只有跑完才可信。
        "positive_valid": bool(expect_any) and (bool(hit) or bool(completed)),
        "hit": hit,
        "negative_ok": not false_positive,
        # 过度触发是观察结果；"没有过度触发"只有跑完才可信。
        "negative_valid": bool(false_positive) or bool(completed),
        "false_positive": false_positive,
    }


TURN_FAILED = "turn_failed"


def turn_failed(events):
    """这一轮到底跑起来没有。

    2026-08-31 第一次真机冒烟就撞上网关 503：harness 如实报「读到 —」，于是
    显示成「正向 0/1」—— 与真正的触发失败**完全无法区分**。跑失败必须单独
    归类，绝不能进正/负分母，否则一次网关抖动就能伪造出一条"路由变差了"的
    结论（AGENTS.md §5：报告结论前先实测，且要能分辨实测到底跑没跑）。
    """
    return any((row.get("kind") if isinstance(row, dict) else None) == TURN_FAILED
               for row in events or ())


def summarize(results):
    """两条轴各自按 valid 计分；不可判定的单列，不摊进任何分母。"""
    positives = [r for r in results
                 if r["positive_ok"] is not None and r.get("positive_valid")]
    negatives = [r for r in results if r.get("negative_valid")]
    return {
        "cases": len(results),
        "errored": sum(1 for r in results if r.get("error")),
        "indeterminate": sum(
            1 for r in results
            if (r["positive_ok"] is not None and not r.get("positive_valid"))
            or not r.get("negative_valid")),
        "positive_ok": sum(1 for r in positives if r["positive_ok"]),
        "positive_total": len(positives),
        "negative_ok": sum(1 for r in negatives if r["negative_ok"]),
        "negative_total": len(negatives),
    }
