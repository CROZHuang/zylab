"""请求前上下文投影：原始会话不可变，发送给模型的视图可裁剪。

``messages`` 始终是可审计的原始事实。工具结果老化、摘要和预算省略只发生在
本模块返回的深拷贝上，绝不回写原始列表。
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
import zlib
from dataclasses import dataclass
from datetime import datetime, timezone

from . import attachments


SUMMARY_VERSION = 1
TOKEN_ESTIMATOR = "ascii-chars/4+non-ascii-chars+message-overhead"
# 刚拿到的结果**原样**给模型看。工具在捕获时各自截过一次（tools.MAX_OUT = 30,000 字符），这一档
# 只给忘了截的工具兜底，**不得比捕获上限更严**。以前是 12,000 → 头 4,000 + 尾 2,000：2026-09-20
# 在真实会话上量到 90 次 read_file 里 23 次（26%）模型刚读出来的文件只看得到 6K，中间整段不可见，
# 只能分块重读。
RECENT_LARGE_TOOL_CHARS = 32_000
RECENT_KEEP_HEAD = 20_000
RECENT_KEEP_TAIL = 10_000
OLD_KEEP_TAIL = 200
# 模型自己过去的 tool_call **参数永远原样回放**，老化只动 role=tool 的结果。以前旧轮次的长参数会被
# 收成「头 200 字 + 占位符」：在 7 个真实会话上重放，它只省 4.3% 的输入 token，却是 5 起模仿事故的
# 全部来源（结果占位符 525 处、零模仿——那是环境的口吻，不是模型自己的），还是缓存击穿的大头之一
# （去掉后理想缓存下需重新预填充的 token −20%）。

# 投影占位符的「结构化开头」。生成器在下面（_preview_tool / materialize 的省略块）；工具层的闸
# （tools.prepare）认的也是这个，别各写一份。「参数投影」的生成器已于 2026-09-20 删除，但正则
# 继续认它：旧会话的原始记录里留着模型当年照抄的假占位符，模型看得见就可能再抄。
# 为什么需要闸：占位符曾出现在模型**自己过去的 tool_call 参数**里，等于用它自己的口吻示范
# 「长参数可以这样收尾」。2026-09-20 实查 5 起模仿（735 次长参数调用的 0.68%）：4 条 bash
# 命令写到一半吐出走样的假占位符（引号不闭合，全部 exit 2），1 次 write_file 把一份反馈书
# 的后 2/3 写成了假占位符——模型事后还汇报「五条差距齐了」，磁盘上只有三条。
PROJECTION_MARKER = re.compile(
    r"\[(?:参数投影|工具结果投影)\s+(?:field|tier)=|<context-projection-omissions>")


def canonical_json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))


def sha256(value):
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def estimate_tokens(value):
    """无 tokenizer 时的明确、确定性估算；不冒充 provider 实测值。"""
    if value is None:
        return 0
    text = value if isinstance(value, str) else canonical_json(value)
    ascii_chars = sum(ord(char) < 128 for char in text)
    non_ascii_chars = len(text) - ascii_chars
    return max(1, math.ceil(ascii_chars / 4 + non_ascii_chars))


def estimate_messages(messages):
    return sum(estimate_tokens(m) + 4 for m in messages)


@dataclass(frozen=True)
class Unit:
    """一个不会被切开的 provider 协议单元。"""

    start: int
    end: int
    messages: tuple
    assistant_turn: bool = False


@dataclass
class Projection:
    messages: list
    report: dict

    @property
    def fits(self):
        return bool(self.report.get("fits"))


def _system_prefix(messages):
    end = 0
    while end < len(messages) and messages[end].get("role") == "system":
        end += 1
    return end


def build_units(messages, start=None):
    """解析完整消息单元，并隔离不完整/孤立的 tool 协议消息。"""
    if start is None:
        start = _system_prefix(messages)
    units, issues = [], []
    i = start
    while i < len(messages):
        message = messages[i]
        role = message.get("role")
        calls = message.get("tool_calls") if role == "assistant" else None
        if calls:
            required = [str(c.get("id") or "") for c in calls]
            j = i + 1
            results = []
            while j < len(messages) and messages[j].get("role") == "tool":
                results.append(messages[j])
                j += 1
            answered = [str(m.get("tool_call_id") or "") for m in results]
            missing = [call_id for call_id in required
                       if call_id and answered.count(call_id) != 1]
            malformed = (not required or any(not call_id for call_id in required)
                         or len(set(required)) != len(required))
            if missing or malformed:
                issues.append({
                    "start": i, "end": j,
                    "reason": "incomplete_tool_pair",
                    "missing_tool_call_ids": missing or required,
                })
                i = j
                continue
            selected = [m for m in results
                        if str(m.get("tool_call_id") or "") in set(required)]
            extras = [m for m in results
                      if str(m.get("tool_call_id") or "") not in set(required)]
            if extras:
                issues.append({
                    "start": i + 1, "end": j,
                    "reason": "orphan_tool_result",
                    "tool_call_ids": [m.get("tool_call_id") for m in extras],
                })
            units.append(Unit(i, j, tuple([message] + selected), True))
            i = j
            continue
        if role == "tool":
            issues.append({
                "start": i, "end": i + 1,
                "reason": "orphan_tool_result",
                "tool_call_ids": [message.get("tool_call_id")],
            })
            i += 1
            continue
        units.append(Unit(i, i + 1, (message,), role == "assistant"))
        i += 1
    return units, issues


def group_turns(units):
    """把协议单元提升为完整用户轮次；预算与 compact 只能切这里。"""
    groups, current = [], []

    def finish():
        if not current:
            return
        messages = tuple(m for unit in current for m in unit.messages)
        groups.append(Unit(current[0].start, current[-1].end, messages,
                           any(unit.assistant_turn for unit in current)))
        current.clear()

    for unit in units:
        role = unit.messages[0].get("role") if unit.messages else None
        if role == "user":
            finish()
            current.append(unit)
        elif current:
            current.append(unit)
        else:
            # 恢复出的 assistant-only 单元没有所属 user，独立保留；
            # 不能把所有这类历史粘成一个永远无法 compact 的大组。
            groups.append(unit)
    finish()
    return groups


def _group_projected(items):
    """保留投影后的预览 metadata，同时按完整用户轮次分组。"""
    groups, current = [], []

    def finish():
        if not current:
            return
        source_units = [item["unit"] for item in current]
        unit = Unit(source_units[0].start, source_units[-1].end,
                    tuple(m for source in source_units for m in source.messages),
                    any(source.assistant_turn for source in source_units))
        groups.append({
            "unit": unit,
            "messages": [m for item in current for m in item["messages"]],
            "previews": [p for item in current for p in item["previews"]],
        })
        current.clear()

    for item in items:
        source = item["unit"]
        role = source.messages[0].get("role") if source.messages else None
        if role == "user":
            finish()
            current.append(item)
        elif current:
            current.append(item)
        else:
            groups.append(item)
    finish()
    return groups


def _prefix_sha(messages, start, end):
    return sha256(messages[start:end])


def _boundaries(units, start, total):
    """摘要边界可以落在哪些下标上：不切开任何协议单元的位置。

    单元的**起点和终点**都算。只认终点会漏掉「紧跟在残缺 tool 对后面」的位置——残缺对
    不属于任何单元，它后面那个单元的起点不是任何单元的终点。2026-09-20 实测：边界落在
    一次被打断的调用之后时，刚写好的摘要会被判成 coverage_splits_conversation_turn，
    压缩等于白做。
    """
    marks = {start, total}
    for unit in units:
        marks.add(unit.start)
        marks.add(unit.end)
    return marks


def _blocking_issues(issues, boundary, total):
    """残缺的 tool 对里，哪些真的挡得住在 ``boundary`` 处落一个摘要边界。

    只有「可能还没完」的才挡路：它跨着边界，或者它就是整段记录的**最后一个单元**（工具也许
    还在跑）。埋在历史里的那种——被 Esc 打断、后面早已有别的消息——永远不会再配对；投影层
    本来就把它剔掉并留一条说明。以前这里是「边界之前有任何残缺对就否决」：2026-09-20 实查，
    一个 1,079 条消息、约 44.6 万 token 的真实会话因为第 30 条和第 176 条各有一次被打断的
    工具调用，压缩规划永远返回 None。打断工具调用是家常便饭，这条规则等于判长会话永不压缩。
    摘要绑定了被覆盖前缀的 sha256，所以「校验时看到的历史残缺对」必然在写摘要时就已经在那里。
    """
    return [issue for issue in issues
            if issue["start"] < boundary
            and (issue["end"] > boundary or issue["end"] >= total)]


def validate_summary(messages, summary, units=None):
    """摘要只有在覆盖边界和原始前缀哈希都仍匹配时才可使用。"""
    if not isinstance(summary, dict) or summary.get("status") != "valid":
        return False, "no_valid_summary"
    start = _system_prefix(messages)
    covered_from = summary.get("covered_from", start)
    covered_to = summary.get("covered_to")
    if covered_from != start or not isinstance(covered_to, int):
        return False, "invalid_coverage"
    if not (start < covered_to <= len(messages)):
        return False, "coverage_out_of_range"
    # 边界按**协议单元**校验（用户轮次的边界是它的子集，旧摘要照样有效）。``units``
    # 参数保留只为兼容调用方；自动压缩可以把边界落在一个超长自治轮次的中间——用户原话
    # 由投影单独逐字回放，不靠「整轮保留」来保证。
    protocol_units, issues = build_units(messages, start)
    if covered_to not in _boundaries(protocol_units, start, len(messages)):
        return False, "coverage_splits_conversation_turn"
    if _blocking_issues(issues, covered_to, len(messages)):
        return False, "coverage_crosses_incomplete_tool_pair"
    if summary.get("covered_sha256") != _prefix_sha(
            messages, start, covered_to):
        return False, "covered_prefix_changed"
    if not str(summary.get("content") or "").strip():
        return False, "empty_summary"
    return True, None


def _call_metadata(assistant):
    out = {}
    for call in assistant.get("tool_calls") or []:
        fn = call.get("function") or {}
        out[str(call.get("id") or "")] = {
            "name": str(fn.get("name") or "tool"),
            "arguments": str(fn.get("arguments") or "{}"),
        }
    return out


def tool_result_digest(content):
    """工具结果在占位符里的短摘要；expand_output 用同一个函数把它找回来。"""
    if not isinstance(content, str):
        content = canonical_json(content)
    return hashlib.sha256(content.encode("utf-8", "replace")).hexdigest()[:16]


def find_tool_result(messages, target):
    """按占位符里的 `sha256:…` 找回原始的 role=tool 消息；找不到返回 None。"""
    wanted = str(target or "").strip().lower()
    if wanted.startswith("sha256:"):
        wanted = wanted[len("sha256:"):]
    if not re.fullmatch(r"[0-9a-f]{8,64}", wanted):
        return None
    for message in reversed(list(messages or ())):
        if not isinstance(message, dict) or message.get("role") != "tool":
            continue
        if tool_result_digest(message.get("content") or "").startswith(wanted[:16]):
            return message
    return None


def _preview_tool(content, meta, recent, age_min_chars, age_keep_head):
    if not isinstance(content, str):
        content = canonical_json(content)
    limit = RECENT_LARGE_TOOL_CHARS if recent else age_min_chars
    if len(content) < limit:
        return content, None

    if recent:
        head_n, tail_n, tier = RECENT_KEEP_HEAD, RECENT_KEEP_TAIL, "recent-large"
    else:
        head_n, tail_n, tier = age_keep_head, OLD_KEEP_TAIL, "aged"
    head = content[:head_n].rstrip()
    tail = content[-tail_n:].lstrip() if len(content) > head_n + tail_n else ""
    digest = tool_result_digest(content)
    omitted = max(0, len(content) - len(head) - len(tail))
    exit_match = re.search(r"\[exit\s+(-?\d+)\]\s*$", content)
    if exit_match:
        status = "exit:" + exit_match.group(1)
    elif "超时" in content:
        status = "timeout"
    elif content.startswith(("[执行失败", "[参数错误", "[已拒绝]")):
        status = "error"
    else:
        status = "ok-or-unknown"
    args = " ".join(meta.get("arguments", "{}").split())[:180]
    header = (f"[工具结果投影 tier={tier} tool={meta.get('name', 'tool')} status={status} "
              f"artifact=sha256:{digest} original_chars={len(content):,} "
              f"omitted_chars={omitted:,} args={args}]")
    projected = header + "\n" + head
    if tail:
        projected += "\n…\n" + tail
    # 指针必须是模型**看得见、抄得走**的：以前这里只说「仍保存在会话事件中」，而 expand_output
    # 要的 tool_call_id 占位符里根本没有——2,651 个真实工具结果、525 处占位符，调用次数为 0。
    projected += (f'\n[原文还在：expand_output(target="sha256:{digest}") 按页取回，不必重跑命令；'
                  "文件内容以磁盘为准，直接重读。]")
    # 中等长度结果若加上可解释 header 反而更大，就保持全文；“老化”不能增肥。
    if len(projected) >= len(content):
        return content, None
    return projected, {
        "artifact": f"sha256:{digest}",
        "tool": meta.get("name", "tool"),
        "tier": tier,
        "status": status,
        "original_chars": len(content),
        "projected_chars": len(projected),
        "saved_chars": max(0, len(content) - len(projected)),
    }


def _project_units(units, age_after_turns, age_min_chars, age_keep_head):
    ranks, rank = {}, 0
    for unit in reversed(units):
        if unit.assistant_turn:
            rank += 1
            ranks[unit.start] = rank

    projected = []
    for unit in units:
        messages = copy.deepcopy(list(unit.messages))
        previews = []
        if messages and messages[0].get("tool_calls"):
            metadata = _call_metadata(messages[0])
            recent = ranks.get(unit.start, age_after_turns + 1) <= age_after_turns
            for message in messages[1:]:
                if message.get("role") != "tool":
                    continue
                meta = metadata.get(str(message.get("tool_call_id") or ""), {})
                content, preview = _preview_tool(
                    message.get("content") or "", meta, recent,
                    age_min_chars, age_keep_head)
                message["content"] = content
                if preview:
                    previews.append(preview)
        projected.append({"unit": unit, "messages": messages,
                          "previews": previews})
    return projected


# 压缩后回灌的工作现场。Claude Code 压完会重读最近 5 个文件（各 5,000、共 50,000 token）并带回
# 计划与任务状态；zylab 以前只带回路径清单，模型得自己想起来去重读。内容在**压缩那一刻**从
# 磁盘读出、冻结进摘要记录——投影因此是确定的，不会每次请求都去读盘、也不会打断前缀缓存。
RESTORE_FILES = 5
RESTORE_FILE_CHARS = 16_000
RESTORE_TOTAL_CHARS = 48_000
# 只读过、没改过的文件价值低得多（多半是参考文档），而带回来的每个字之后每次请求都要发：
# 名额和篇幅都收紧。实测一个真实会话里两份只读文档就占了回灌块的 97%。
RESTORE_READONLY_FILES = 2
RESTORE_READONLY_CHARS = 8_000


def _restored_block(summary):
    restored = summary.get("restored") or {}
    parts = []
    files = [item for item in restored.get("files") or [] if item.get("content")]
    if files:
        rows = [
            f"<restored-files at=\"{restored.get('at', '')}\">"
            "压缩那一刻磁盘上的**当前内容**（不是历史快照；后面尾部里出现的修改已经包含在内）。"
            "只带回了最近动过的几个文件，需要别的、或需要被截掉的部分，就重新读。"]
        for item in files:
            note = "，已截断" if item.get("truncated") else ""
            rows.append(f"### {item['path']}（{item.get('chars', 0):,} 字符{note}）\n"
                        + item["content"])
        rows.append("</restored-files>")
        parts.append("\n".join(rows))
    plan = str(restored.get("task_plan") or "").strip()
    if plan:
        parts.append("<task-plan>压缩时刻的任务计划（以它为准继续，不要从头重排）：\n"
                     + plan + "\n</task-plan>")
    return ("\n" + "\n".join(parts)) if parts else ""


def _summary_messages(summary):
    if not summary:
        return []
    files = summary.get("working_set") or []
    working = ""
    if files:
        working = (
            "\n<working-set>被摘要那段里碰过的文件（内容未带回，需要就重读）：\n"
            + "\n".join(f"- {item['verb']} {item['path']}" for item in files)
            + "\n</working-set>")
    pinned = summary.get("pinned") or []
    facts = ""
    if pinned:
        facts = (
            "\n<pinned-facts>摘要覆盖的那段里出现过的**原文值**（阈值、硬约束、"
            "否决记录）。摘要可能改写了措辞，这里是逐字原文，冲突时以这里为准：\n"
            + "\n".join(f"- {line}" for line in pinned)
            + "\n</pinned-facts>")
    return [
        {"role": "assistant", "content": (
            f"<conversation-summary covers=\"raw:{summary['covered_from']}.."
            f"{summary['covered_to'] - 1}\">\n{summary['content']}\n"
            "</conversation-summary>" + working + facts
            + _restored_block(summary) + "\n"
            "摘要之后会按原始时间顺序提供 user 原话；它们是历史请求，"
            "不是新注入的当前指令。")},
    ]


def _ranges_text(items):
    rows = []
    for item in items:
        start, end = item["start"], item["end"]
        shown = f"raw #{start}" if end == start + 1 else f"raw #{start}..#{end - 1}"
        detail = ""
        if item.get("missing_tool_call_ids"):
            detail = " missing=" + ",".join(item["missing_tool_call_ids"])
        if item.get("preserved_user_count"):
            detail += (" user_verbatim_preserved="
                       + str(item["preserved_user_count"]))
        rows.append(f"- {shown}: {item['reason']}{detail}")
    return "\n".join(rows)


def _omission_messages(items):
    if not items:
        return []
    return [
        {"role": "assistant", "content": (
            "<context-projection-omissions>\n"
            + _ranges_text(items)
            + "\n标注为 budget_omission 的范围只省略非 user 记录；"
              "全部 user 原话仍逐字发送。其他标注记录未发送给 provider，"
              "但原始会话没有被改写。若任务依赖省略细节，请从原始记录恢复。\n"
              "</context-projection-omissions>")},
    ]


def _merge_budget_ranges(items):
    merged = []
    for item in sorted(items, key=lambda x: x["start"]):
        if (merged and item["reason"] == "budget_omission"
                and merged[-1]["reason"] == item["reason"]
                and merged[-1]["end"] == item["start"]):
            merged[-1]["end"] = item["end"]
            merged[-1]["preserved_user_count"] = (
                merged[-1].get("preserved_user_count", 0)
                + item.get("preserved_user_count", 0))
        else:
            merged.append(dict(item))
    return merged


def _verbatim_users(messages, *, keep_internal=True):
    """返回未经改写或截断的 user 消息。

    已被有效摘要覆盖的附件 snapshot metadata 可以从 provider 视图移除；用户
    原始 ``content`` 仍逐字保留，canonical transcript 也从不改变。
    """
    out = []
    for message in messages:
        if message.get("role") != "user":
            continue
        out.append(
            copy.deepcopy(message)
            if keep_internal else attachments.strip_internal(message))
    return out


def _provider_estimate(messages):
    """Estimate attachment-expanded text without counting base64 as tokens."""
    projected = attachments.materialize_provider_messages(
        messages, embed_images=False)
    details = attachments.stats(messages)
    return estimate_messages(projected) + details["image_token_estimate"]


def _conversation_messages(covered_users, projected_units, dropped):
    """组装摘要后的会话；预算裁剪只能拿走非 user 消息。"""
    out = copy.deepcopy(covered_users)
    for item in projected_units:
        messages = item["messages"]
        if item["unit"].start in dropped:
            out.extend(_verbatim_users(messages))
        else:
            out.extend(copy.deepcopy(messages))
    return out


def _assemble(system, summary_messages, covered_users, projected_units,
              dropped, omissions):
    # synthetic 控制信息使用 assistant role 并放在 raw user 之前；这样不会把
    # 模型生成的摘要提升成高权限指令，也不会污染 provider 的 user 原话序列。
    return (copy.deepcopy(system) + copy.deepcopy(summary_messages)
            + _omission_messages(omissions)
            + _conversation_messages(covered_users, projected_units, dropped))


def materialize(messages, *, summary=None, tools_schema=None,
                control_messages=None, model_limit=256_000,
                usable_budget=None, model_limit_source="unknown",
                last_provider_tokens=0, age_after_turns=3,
                age_min_chars=400, age_keep_head=200):
    """生成一次 provider 请求视图，并返回可解释预算报告。"""
    raw = list(messages or [])
    raw_digest = sha256(raw)
    start = _system_prefix(raw)
    controls = copy.deepcopy(list(control_messages or []))
    if any(message.get("role") != "system" for message in controls):
        raise ValueError("control_messages 只接受 role=system")
    system = raw[:start] + controls
    protocol_units, integrity = build_units(raw, start)
    turns = group_turns(protocol_units)

    valid, invalid_reason = validate_summary(raw, summary, turns)
    active_summary = summary if valid else None
    coverage = active_summary["covered_to"] if active_summary else start
    remaining = [u for u in protocol_units if u.start >= coverage]
    projected_units = _group_projected(_project_units(
        remaining, age_after_turns, age_min_chars, age_keep_head))
    summary_msgs = _summary_messages(active_summary)
    covered_users = _verbatim_users(
        raw[start:coverage], keep_internal=False)

    model_limit = max(1, int(model_limit or 1))
    if usable_budget is None:
        usable_budget = max(1, model_limit - min(64_000, int(model_limit * 0.15)))
    usable_budget = max(1, min(model_limit, int(usable_budget)))
    tools_tokens = estimate_tokens(tools_schema or [])

    # 已经被摘要覆盖的残缺对不再列进省略说明：那段历史整体由摘要代表了，
    # 再提「raw #30 缺 tool 结果」对模型只是噪音。（report 里的完整清单不变。）
    omissions = _merge_budget_ranges([
        dict(item) for item in integrity if item["end"] > coverage])
    fixed_tokens = (estimate_messages(system) + estimate_messages(summary_msgs)
                    + _provider_estimate(covered_users) + tools_tokens)
    unit_tokens = []
    for item in projected_units:
        total = _provider_estimate(item["messages"])
        omittable = estimate_messages([
            message for message in item["messages"]
            if message.get("role") != "user"
        ])
        unit_tokens.append((item, total, omittable))
    kept_tokens = sum(total for _, total, _ in unit_tokens)
    marker_tokens = estimate_messages(_omission_messages(omissions))
    untrimmed_tokens = fixed_tokens + kept_tokens + marker_tokens

    protected = set()
    if projected_units:
        protected.add(projected_units[-1]["unit"].start)
        for item in reversed(projected_units):
            if any(m.get("role") == "user" for m in item["messages"]):
                protected.add(item["unit"].start)
                break

    # 只维护 token 加总与一个很小的 range marker；不在每删一轮后深拷贝
    # 整段历史。长会话因此是 O(n)，而不是原先的 O(n²)。
    dropped = set()
    current_tokens = untrimmed_tokens
    for candidate, _, omittable_tokens in unit_tokens:
        if current_tokens <= usable_budget:
            break
        start_index = candidate["unit"].start
        if start_index in protected or not omittable_tokens:
            continue
        dropped.add(start_index)
        kept_tokens -= omittable_tokens
        preserved_user_count = len(_verbatim_users(candidate["messages"]))
        omissions = _merge_budget_ranges(omissions + [{
            "start": start_index, "end": candidate["unit"].end,
            "reason": "budget_omission",
            "preserved_user_count": preserved_user_count,
        }])
        marker_tokens = estimate_messages(_omission_messages(omissions))
        current_tokens = fixed_tokens + kept_tokens + marker_tokens
    kept = [item for item in projected_units
            if item["unit"].start not in dropped]

    final_messages = _assemble(
        system, summary_msgs, covered_users, projected_units, dropped, omissions)
    raw_users = _verbatim_users(raw)
    projected_users = _verbatim_users(final_messages)
    raw_user_content = [message.get("content") for message in raw_users]
    projected_user_content = [
        message.get("content") for message in projected_users]
    if projected_user_content != raw_user_content:
        raise RuntimeError("context projection violated verbatim user invariant")
    provider_messages = attachments.materialize_provider_messages(
        final_messages, embed_images=True)
    provider_estimation_messages = attachments.materialize_provider_messages(
        final_messages, embed_images=False)
    attachment_details = attachments.stats(final_messages)
    canonical_estimated = estimate_messages([
        attachments.strip_internal(message) for message in final_messages])
    provider_text_estimated = estimate_messages(provider_estimation_messages)
    attachment_text_tokens = max(
        0, provider_text_estimated - canonical_estimated)
    estimated = (
        provider_text_estimated
        + attachment_details["image_token_estimate"]
        + tools_tokens)
    fits = estimated <= usable_budget
    previews = [p for item in kept for p in item["previews"]]
    tool_messages = [m for item in kept for m in item["messages"]
                     if m.get("role") == "tool"]
    preview_tool_messages = [m for m in tool_messages
                             if str(m.get("content") or "").startswith(
                                 "[工具结果投影")]
    full_tool_messages = [m for m in tool_messages
                          if m not in preview_tool_messages]
    preview_tokens = estimate_messages(preview_tool_messages)
    full_tool_tokens = estimate_messages(full_tool_messages)
    conversation = _conversation_messages(covered_users, projected_units, dropped)
    non_user_non_tool = [m for m in conversation
                         if m.get("role") not in ("user", "tool")]
    user_tokens = estimate_messages([
        attachments.strip_internal(message) for message in projected_users])
    reserve = model_limit - usable_budget
    output_reserve = min(8_192, reserve)
    tool_reserve = min(32_000, max(0, reserve - output_reserve) * 2 // 3)
    safety_reserve = max(0, reserve - output_reserve - tool_reserve)
    summary_report = {
        "status": "valid" if active_summary else "none",
        "invalid_reason": None if active_summary else invalid_reason,
        "covered_from": active_summary.get("covered_from") if active_summary else None,
        "covered_to": active_summary.get("covered_to") if active_summary else None,
        "tokens": estimate_messages(summary_msgs),
    }
    components = {
        "system": estimate_messages(system),
        "tools_schema": tools_tokens,
        "summary": estimate_messages(summary_msgs),
        "verbatim_users": user_tokens,
        "attachments": attachment_text_tokens,
        "image_inputs": attachment_details["image_token_estimate"],
        "recent_messages": estimate_messages(non_user_non_tool),
        "tool_results": full_tool_tokens,
        "tool_previews": preview_tokens,
        "omission_marker": estimate_messages(_omission_messages(omissions)),
        "reserved_output": output_reserve,
        "reserved_tools": tool_reserve,
        "reserved_safety": safety_reserve,
    }
    largest = sorted(previews, key=lambda p: p["original_chars"], reverse=True)[:5]
    if not fits:
        next_action = ("protected/verbatim context cannot fit; shorten the latest input, "
                       "start a new session, or choose a larger model")
    elif omissions:
        next_action = "projection is usable; recover omitted raw ranges if their details become relevant"
    elif estimated > int(usable_budget * 0.8):
        next_action = "near budget; compact before the next large tool result"
    else:
        next_action = "none"
    report = {
        "version": 2,
        "raw_sha256": raw_digest,
        "raw_message_count": len(raw),
        "canonical_projected_sha256": sha256(final_messages),
        "projected_sha256": sha256(provider_messages),
        "projected_message_count": len(provider_messages),
        "model_limit": model_limit,
        "model_limit_source": model_limit_source,
        "usable_budget": usable_budget,
        "estimated_request_tokens": estimated,
        "untrimmed_estimated_tokens": untrimmed_tokens,
        "token_estimator": TOKEN_ESTIMATOR,
        "last_provider_tokens": int(last_provider_tokens or 0),
        "last_provider_tokens_source": (
            "provider_usage" if last_provider_tokens else "not_available"),
        "fits": fits,
        "summary": summary_report,
        "verbatim_users": {
            "count": len(projected_users),
            "tokens": user_tokens,
            "truncated": 0,
            "preserved": True,
        },
        "components": components,
        "tool_previews": {
            "count": len(previews),
            "saved_chars": sum(p["saved_chars"] for p in previews),
            # raw transcript 每个 tool loop 都会变化；这个 fingerprint 只描述
            # 实际被投影的工具集合，供 UI 去重 aging notice。
            "fingerprint_sha256": sha256(previews),
            "largest_artifacts": largest,
        },
        "attachments": attachment_details,
        "incomplete_tool_pairs": integrity,
        "omitted_ranges": omissions,
        "next_action": next_action,
    }
    return Projection(provider_messages, report)


def _assemble_plan(raw, start, active, covered, tail_start):
    """把选定的轮次组装成一个 plan；covered 是要摘要的 unit 列表。"""
    source = []
    if active:
        source.append({"role": "summary", "content": active["content"]})
    for unit in covered:
        source.extend(copy.deepcopy(list(unit.messages)))
    inherited = _inherited(active)
    users = sum(1 for unit in covered for message in unit.messages
                if message.get("role") == "user")
    return {
        "covered_from": start,
        "covered_to": tail_start,
        "covered_sha256": _prefix_sha(raw, start, tail_start),
        "source_messages": source,
        # 「几个用户轮次」= 覆盖范围里的 user 消息数；covered 可能是轮次，也可能是协议单元
        "source_turns": users,
        "source_units": len(covered),
        "source_message_count": len(source),
        "inherited": inherited,
    }


TAIL_MIN_UNITS = 2


def _tail_cost(unit):
    """这个单元留在尾部时大约占多少 token：工具结果按「新鲜大结果」的投影上限计。"""
    keep = RECENT_KEEP_HEAD + RECENT_KEEP_TAIL
    total = 0
    for message in unit.messages:
        content = message.get("content")
        if (message.get("role") == "tool" and isinstance(content, str)
                and len(content) > keep):
            total += estimate_tokens(content[:keep]) + 4
        else:
            total += estimate_tokens(message) + 4
    return total


def _tail_start_by_tokens(units, turns, tail_tokens):
    """按 token 预算从后往前留尾部，返回尾部第一条消息的下标。

    以前的规则是「留最近 6 个用户轮次」。自治程度越高的会话用户轮次越少：2026-09-20 在
    真实会话上重放，一个 284 条消息、约 9.8 万 token 的会话只有 6 个用户轮次——**永远
    压不了**；一个 410 条的会话一段只能覆盖前 4 条。Claude Code 以 API 轮次为单位，Codex
    压不下就从最旧的条目丢；共同点是压缩在任何时刻都能发生。
    这里按**协议单元**（一条 assistant 消息连同它的工具结果）量尾部；能落在用户轮次的
    起点上就落在那里（切口干净），只有单个轮次自己就超过预算时才切进轮次内部。用户原话
    不受影响：被覆盖的那段里的 user 消息由投影逐字回放。
    """
    if not units:
        return None
    budget = max(1, int(tail_tokens))
    used, cut = 0, len(units)
    for index in range(len(units) - 1, -1, -1):
        cost = _tail_cost(units[index])
        if len(units) - index > TAIL_MIN_UNITS and used + cost > budget:
            break
        used += cost
        cut = index
    unit_cut = units[cut].start
    # 同一预算内最靠前的用户轮次起点；太靠后（尾部不到预算的 1/4）就不迁就它。
    costs = {unit.start: _tail_cost(unit) for unit in units[cut:]}
    for turn in turns:
        if turn.start < unit_cut:
            continue
        kept = sum(cost for start, cost in costs.items() if start >= turn.start)
        if kept * 4 >= min(budget, used):
            return turn.start
        break
    return unit_cut


def plan_compaction(messages, summary=None, keep_tail=6,
                    max_source_tokens=None, tail_tokens=None):
    """选择摘要边界；返回生成摘要所需的原文，或 ``None``。

    ``tail_tokens``：按 token 预算留尾部、以协议单元为粒度（自动 / 手动压缩用这个，
    见 ``_tail_start_by_tokens``）。不给则沿用「留最近 ``keep_tail`` 个用户轮次」。

    ``max_source_tokens`` 给摘要请求的**输入**设上限。为什么需要：一次把 143 轮
    压成 138K token 的提示，provider 要跑两三分钟，模型也更容易写偏（实测把
    「整理摘要」本身当成了 objective）。设上限后每次只覆盖装得下的一段，剩下的
    靠摘要链在下一次接着压 —— 摘要本来就会作为 source 的第一条参与下一轮。
    """
    raw = list(messages or [])
    start = _system_prefix(raw)
    protocol_units, issues = build_units(raw, start)
    turns = group_turns(protocol_units)
    valid, _ = validate_summary(raw, summary, turns)
    active = summary if valid else None
    coverage = active["covered_to"] if active else start
    if tail_tokens:
        pending = [u for u in protocol_units if u.start >= coverage]
        tail_start = _tail_start_by_tokens(
            pending, [t for t in turns if t.start >= coverage], tail_tokens)
        if tail_start is None or tail_start <= coverage:
            return None
        if _blocking_issues(issues, tail_start, len(raw)):
            return None
        covered = [unit for unit in pending if unit.end <= tail_start]
        if not covered:
            return None
        return _bounded_plan(raw, start, active, covered, tail_start,
                             max_source_tokens)
    remaining = [u for u in turns if u.start >= coverage]
    if keep_tail <= 0:
        # keep_tail=0：调用方不拿摘要替换上下文，所以不需要给尾部留原文，
        # 覆盖到最后一个**完整**轮次为止即可。
        if not remaining:
            return None
        tail_start = remaining[-1].end
    else:
        if len(remaining) <= keep_tail:
            return None
        tail_start = remaining[-keep_tail].start
    if tail_start <= coverage:
        return None
    if _blocking_issues(issues, tail_start, len(raw)):
        return None
    covered = [unit for unit in remaining if unit.end <= tail_start]
    if keep_tail <= 0 and covered:
        tail_start = covered[-1].end
    if not covered:
        return None
    return _bounded_plan(raw, start, active, covered, tail_start,
                         max_source_tokens)


def _bounded_plan(raw, start, active, covered, tail_start, max_source_tokens):
    if not max_source_tokens:
        return _assemble_plan(raw, start, active, covered, tail_start)

    # 二分找「渲染后仍不超预算」的最长前缀。渲染而不是估算原文，因为
    # summary_prompt 会把工具结果截到 600 字，两者能差一个数量级。
    def fits(count):
        # 这一段的终点 = 下一个单元的起点：中间若夹着残缺 tool 对，边界落在它后面，
        # 让那段空隙也算进覆盖范围（否则它会永远挂在投影的省略说明里）。
        end = covered[count].start if count < len(covered) else tail_start
        plan = _assemble_plan(raw, start, active, covered[:count], end)
        return estimate_tokens(summary_prompt(plan)) <= max_source_tokens, plan

    ok, plan = fits(len(covered))
    if ok:
        return plan
    low, high, best = 1, len(covered) - 1, None
    while low <= high:
        mid = (low + high) // 2
        ok, plan = fits(mid)
        if ok:
            best, low = plan, mid + 1
        else:
            high = mid - 1
    # 一轮都装不下也要压：它已经是最小的完整单位，硬压好过永远压不动。
    return best if best is not None else fits(1)[1]


def plan_compaction_to(messages, covered_to, summary=None):
    """Plan an exact prefix summary at a complete conversation boundary.

    This is the checkpoint-facing counterpart to ``plan_compaction``.  It
    never mutates raw messages and refuses a cutoff that splits a user turn or
    crosses an incomplete tool protocol pair.
    """
    raw = list(messages or [])
    start = _system_prefix(raw)
    if type(covered_to) is not int:
        raise ValueError("summary cutoff 必须是整数")
    if not start < covered_to <= len(raw):
        raise ValueError(
            f"summary cutoff 超出范围：{covered_to}（有效 {start + 1}..{len(raw)}）")

    protocol_units, issues = build_units(raw, start)
    turns = group_turns(protocol_units)
    if _blocking_issues(issues, covered_to, len(raw)):
        raise ValueError("summary cutoff 跨过不完整 tool call/result")
    if covered_to not in _boundaries(turns, start, len(raw)) - {start}:
        raise ValueError("summary cutoff 会切断完整用户轮次")

    valid, _ = validate_summary(raw, summary, turns)
    active = summary if valid else None
    if active and active["covered_to"] == covered_to:
        return None
    if active and active["covered_to"] > covered_to:
        active = None
    coverage = active["covered_to"] if active else start
    selected = [
        unit for unit in turns
        if unit.start >= coverage and unit.end <= covered_to
    ]
    if not selected:
        return None
    source = []
    if active:
        source.append({"role": "summary", "content": active["content"]})
    for unit in selected:
        source.extend(copy.deepcopy(list(unit.messages)))
    return {
        "covered_from": start,
        "covered_to": covered_to,
        "covered_sha256": _prefix_sha(raw, start, covered_to),
        "source_messages": source,
        "source_turns": len(selected),
        "source_message_count": len(source),
        "inherited": _inherited(active),
    }


def _inherited(active):
    """上一代摘要里**不在正文里**的结构化部分。链式压缩时新摘要的 source 只有旧摘要的
    正文（一条伪消息），`pinned` 和 `working_set` 是另存的——不显式带过来就在第二代蒸发了
    （2026-09-20 执行过的测试证实）。阈值、硬约束、否决记录恰恰是最不该随代数衰减的东西。"""
    if not active:
        return {}
    return {"pinned": list(active.get("pinned") or []),
            "working_set": [dict(item) for item in active.get("working_set") or []]}


def plan_anchor_lines(plan):
    """这一代要逐字带回的值 = 上一代带回来的 + 这一段新出现的，同一套打分与预算。"""
    inherited = (plan.get("inherited") or {}).get("pinned") or []
    carried = [{"role": "summary", "content": "\n".join(inherited)}] if inherited else []
    return anchor_lines(carried + list(plan.get("source_messages") or []))


def plan_working_set(plan):
    """工作集同理：旧的在前、新的在后，同一路径以最后一次为准，「改」压过「读」。"""
    merged = {}
    rows = list((plan.get("inherited") or {}).get("working_set") or [])
    rows += working_set(plan.get("source_messages"))
    for item in rows:
        path, verb = item.get("path"), item.get("verb")
        if not path:
            continue
        previous = merged.pop(path, None)
        merged[path] = "改" if "改" in (previous or "", verb or "") else (verb or previous)
    return _trim_working_set(merged)


SUMMARY_HEADINGS = ("objective", "constraints", "decisions", "files_changed",
                    "evidence", "pending", "risks")
_HEADING = re.compile(r"^#{1,3}\s*([a-z_]+)\b", re.I | re.M)


def summary_defects(text, finish_reason=None):
    """这份摘要能不能装上去。返回缺陷代码列表，空 = 可以。

    摘要一旦装上，就**永久替换**了模型眼里的那段历史，所以宁可拒收。以前「只要非空就接受」：
    2026-09-20 在真实网关上测到一份写满 6000 token 后陷入重复循环的摘要（「见 evidence 段。
    见 evidence 段。…」，七个标题只有四个，漏掉 15 个锚定值）——它会被原样装上去。

      truncated   输出额度用尽（finish_reason=length）：后半截没写完，多半缺 pending / risks
      degenerate  陷入重复：尾部 1500 字的压缩比 < 0.06（正常文字约 0.6，规整的要点列表约 0.1，
                  循环约 0.02），或同一句话在尾部出现 ≥ 10 次
      structure   规定的七个标题少于 5 个，或没有 objective
    """
    text = str(text or "").strip()
    defects = []
    if str(finish_reason or "") == "length":
        defects.append("truncated")
    tail = text[-1500:]
    if len(tail) >= 400:
        raw = tail.encode("utf-8")
        counts = {}
        for piece in re.split(r"[。\n]", tail):
            piece = piece.strip()
            if len(piece) >= 4:
                counts[piece] = counts.get(piece, 0) + 1
        if (len(zlib.compress(raw, 9)) / len(raw) < 0.06
                or max(counts.values(), default=0) >= 10):
            defects.append("degenerate")
    found = {name.lower() for name in _HEADING.findall(text)}
    present = [name for name in SUMMARY_HEADINGS if name in found]
    if len(present) < 5 or "objective" not in found:
        defects.append("structure")
    return defects


DEFECT_TEXT = {
    "truncated": "输出被额度截断",
    "degenerate": "正文陷入重复",
    "structure": "缺少规定的标题",
}


def summary_prompt(plan, instructions=None):
    rows = []
    # Text snapshots are expanded for compaction, otherwise a historical
    # ``@paper.txt`` would collapse to a path token and lose its evidence.
    source_messages = attachments.materialize_provider_messages(
        plan["source_messages"], embed_images=False)
    turn = 0
    for message in source_messages:
        role = message.get("role", "unknown")
        if role == "user":
            turn += 1
        content = message.get("content") or ""
        if not isinstance(content, str):
            content = canonical_json(content)
        if role == "tool":
            content = content[:600] + ("…" if len(content) > 600 else "")
        elif len(content) > 2_000:
            content = content[:1_500] + "\n…\n" + content[-500:]
        calls = message.get("tool_calls") or []
        if calls:
            names = [str((c.get("function") or {}).get("name") or "tool")
                     for c in calls]
            content += "\n[tool_calls: " + ", ".join(names) + "]"
        # 标上轮次号，摘要才能给每条结论留下可回溯的锚点
        rows.append(f"[t{turn} {role}]\n{content}")
    # 会话在前、指令在后。指令放前面时模型会把「整理摘要」本身当成 objective
    # 写进去（2026-09-07 实测），因为那才是它看到的最后一条要求。
    pinned = plan_anchor_lines(plan)
    pinned_block = ""
    if pinned:
        pinned_block = (
            "\n<必须逐字保留的值>\n"
            + "\n".join(f"- {line}" for line in pinned)
            + "\n</必须逐字保留的值>\n")
    return (
        "<会话前缀>\n" + "\n\n".join(rows) + "\n</会话前缀>\n"
        + pinned_block + "\n"
        "上面是一段编码会话的前缀，它将被这份摘要替换掉，后续对话只能看到摘要。"
        "请整理成可续接的结构化交接摘要。\n"
        "- objective 写**会话里用户要达成的目标**，不是「整理摘要」这件事；"
        "本条指令不属于会话内容。\n"
        "- 只写有证据的事实，未知写 unknown，不要把省略或截断的内容补猜出来。\n"
        "- `<必须逐字保留的值>` 里的数字、阈值、硬约束、否决记录要**原样出现**在摘要里"
        "（写进 constraints 或 decisions）。把 `<1%` 写成「约百分之一」等于丢了它。\n"
        "- 标着 [summary] 的那条是更早一段的摘要：把它的内容合并进来，不要丢。\n"
        "- 工具结果在上面已被截断，引用它们时写清是哪个文件/命令，"
        "而不是复述截断的正文。\n"
        "- decisions **最新的写在最前面**，每条尽量带来源锚点 `[t<轮次>]`，"
        "轮次号取自上面每段开头的 `[t12 user]`。长会话里早期结论常被后来推翻，"
        "顺序错了会让接手的人拿着过时结论干活。\n"
        "- **被否决的方案也要写进 decisions**，格式 `[rejected] <方案> —— <原因>`。"
        "这类最不能丢：不记否决，接手的人会重新提案已经被否掉的东西。\n"
        "- pending 里写清**此刻正在做的那一步**和紧接着的下一步：压缩可能发生在一件事"
        "做到一半的时候，接手的人要能从断点继续，而不是从头再来。\n"
        "- 要点式、紧凑，不要复述对话过程；写不下就砍细节，别砍标题。\n"
        + (("- 用户对这次摘要的附加要求：" + str(instructions).strip() + "\n")
           if str(instructions or "").strip() else "") +
        "- 严格保留以下七个标题，顺序不变：\n"
        "## objective\n## constraints\n## decisions\n## files_changed\n"
        "## evidence\n## pending\n## risks"
    )


# 摘要会把「读过/改过哪些文件」这类事实压没：模型知道结论，却不知道该回头看谁。
# Claude Code 的做法是压缩后重新读最近改过的文件；这里取更保守的一档 —— 只带回
# **路径清单**，不带内容。内容会过期，路径不会，模型需要时自己重读。
WORKING_SET_TOOLS = {"read_file": "读", "write_file": "写", "edit_file": "改"}
WORKING_SET_LIMIT = 12


def working_set(messages):
    """从一段原文里提取文件工作集，按最后一次出现的顺序（新的在后）。"""
    seen = {}
    for message in messages or []:
        for call in message.get("tool_calls") or []:
            function = call.get("function") or {}
            verb = WORKING_SET_TOOLS.get(str(function.get("name") or ""))
            if not verb:
                continue
            try:
                args = json.loads(function.get("arguments") or "{}")
            except (ValueError, TypeError):
                continue
            path = args.get("path") or args.get("file_path") or args.get("file")
            if not isinstance(path, str) or not path.strip():
                continue
            # 同一文件既读又写时保留「写/改」——那才是要交接的事实
            previous = seen.pop(path, None)
            seen[path] = "改" if "改" in (previous or "", verb) else verb
    return _trim_working_set(seen)


def _trim_working_set(seen):
    """有界清单里**改过 / 写过的文件优先于只读过的**：位置不够时先让出「读」。

    只按「最后碰过」排的话，早先改过的文件会被后面一串只读的路径挤掉——而要交接的
    事实恰恰是「我动过哪些文件」。输出仍按最后一次出现的顺序（新的在后）。
    """
    items = list(seen.items())
    modified = [path for path, verb in items if verb != "读"][-WORKING_SET_LIMIT:]
    room = WORKING_SET_LIMIT - len(modified)
    reads = [path for path, verb in items if verb == "读"]
    keep = set(modified) | set(reads[-room:] if room > 0 else [])
    return [{"path": path, "verb": verb} for path, verb in items if path in keep]


# --- 高信号内容锚定 ------------------------------------------------------
# 为什么需要：`summary_prompt` 在模型看到任何东西之前，就把工具结果截到 600 字符、
# 其余内容截到 1500+500。一条 3,000 字符结果里位于中段的「阈值 <1% 视为通过」因此
# **根本不会进入摘要请求** —— 模型保不住它没见过的东西。所以把这类「值」按行先摘
# 出来：既塞进提示（要求逐字复述），也独立随摘要记录带回（不依赖模型是否照做）。
#
# 与 `<working-set>` 的分工：那个只带路径不带内容，理由是「内容会过期，路径不会」。
# 这里相反 —— 阈值、极性、硬约束**不会过期**，一个 `<1%` 永远是 `<1%`，所以值得
# 逐字带回。
#
# 只认「值」，不认叙述：带比较符/百分号的数字、显式硬约束词、否决标记。范围刻意
# 窄 —— 放宽就等于不截断，会把摘要请求重新撑爆（那正是 2026-09-07 故障的成因）。
# 模式要带边界，否则真实数据里全是假阳性（2026-09-17 在真实会话上实测：
# 头 6 条锚定里 5 条是垃圾）。两类假阳性各有来头：
#   `{c['inputTokens']:>10,}`  f-string 的对齐格式，`:>10` 长得像「大于 10」
#   `...s1nv8%tmp%x.py`        URL 编码的路径，`8%` 长得像百分数
# 预算只有 24 行，垃圾会把真正的阈值挤出去 —— 所以宁可漏也不能滥。
# 负向回顾里必须含 `-` `=` `<` `>`：`internal.example -> 10.12.111.139`
# 在真实会话上被命中过 `> 1`（箭头里的 `>`），`=>` `<-` 同理。
# `>=24.0.0` 这种版本约束仍然保留 —— 它前面是空格，是正当的要求。
_CMP = re.compile(r"(?<![:{\w=<>-])[<>≤≥]=?\s*\d")
_PCT = re.compile(r"(?<![\w.])\d+(?:\.\d+)?\s*[%％](?![\w])")
_RULE = re.compile(r"必须|不得|禁止|一律|绝不|务必")
_REJECTED = re.compile(r"\[rejected\]", re.I)
ANCHOR_PATTERNS = (_CMP, _PCT, _RULE, _REJECTED)

# 明显不是「判据」的行：目录列表、纯源码。注释行**不排除** —— 代码注释里常写着
# 真正的约束（「时间戳必须带亚秒」就是一条）。
_ANCHOR_NOISE = (
    re.compile(r"^[-dbclps][rwxsStT-]{9}"),                 # ls -l 的权限位
    re.compile(r"^[0-9a-fA-F]{16,}$"),                      # 裸哈希
)
# 源码里的比较式和阈值长得一模一样（`if len(parts) < 2:` / `or row.get(..) < 0`），
# 2026-09-17 真实会话上实测占了 24 条里的 9 条。两条判据都**不看语言**，
# 免得机制只对中文项目有效：
#   ① 行首是语句关键字 —— 要先剥掉 `grep -n` 的 `405:` 行号前缀
#   ② 结构字符密度高 —— `hours[str(int(ts[11:13]))] += 1 if ...` 这类表达式
_CODE_LEAD = re.compile(
    r"^\s*(?:\d+:\s*)?(?:if|elif|else|for|while|with|try|except|finally|"
    r"or|and|not|return|yield|assert|print|import|from|def|class|raise|"
    r"lambda|self\.|await|async)\b")
_CODE_CHARS = re.compile(r"[(){}\[\]=;]")
_CODE_DENSITY = 4
ANCHOR_MAX_LINES = 24
ANCHOR_MAX_CHARS = 2_000
ANCHOR_LINE_CHARS = 200


def _anchor_key(line):
    """去重键：**命中的值本身**，不是整行。

    同一个事实常有多种措辞 —— 真实会话里「省 44%」出现了三种写法、「<1%」三次，
    24 格预算里被重复占掉近一半。按整行去重留不住它们。

    已知代价（不假装没有）：两个不相干的事实若共用同一个数字（「缓存率 70%」与
    「覆盖率 70%」）会塌成一条，保留分数更高的那个。这比留三条同义复述好 ——
    预算是硬的。
    """
    values = set()
    for pattern in (_CMP, _PCT):
        for found in pattern.finditer(line):
            values.add(found.group(0).replace(" ", ""))
    if values:
        return ("value",) + tuple(sorted(values))
    return ("line", line)


def _anchor_score(line):
    """行的判据价值：既有硬约束词又有数字的最值钱，纯硬约束词最便宜。

    按分数填预算而不是先到先得 —— 真实会话里噪声出现得比判据早，先到先得等于
    把 24 行的额度让给文件名。
    """
    has_value = bool(_CMP.search(line) or _PCT.search(line))
    has_rule = bool(_RULE.search(line))
    if _REJECTED.search(line):
        return 3
    if has_value and has_rule:
        return 3
    if has_value:
        return 2
    if has_rule:
        return 1
    return 0


def _anchor_worthy(line):
    if any(pattern.search(line) for pattern in _ANCHOR_NOISE):
        return False
    if _CODE_LEAD.search(line):
        return False
    if len(_CODE_CHARS.findall(line)) >= _CODE_DENSITY:
        return False
    return _anchor_score(line) > 0


def anchor_lines(messages):
    """从一段原文里摘出「值」级事实：先全收，再按判据价值填有界预算。

    刻意只看行：一行里同时有上下文和数字，比抽出裸数字（`1%` 脱离语境毫无意义）
    可用得多，又比整段保留便宜。
    """
    seen = []
    index = 0
    for message in messages or []:
        content = message.get("content") or ""
        if not isinstance(content, str):
            continue
        for raw_line in content.splitlines():
            line = raw_line.strip()
            if not line or not _anchor_worthy(line):
                continue
            if len(line) > ANCHOR_LINE_CHARS:
                line = line[:ANCHOR_LINE_CHARS] + "…"
            seen.append((-_anchor_score(line), index, line))
            index += 1
    out = []
    used = 0
    taken = set()
    for _, _, line in sorted(seen):
        key = _anchor_key(line)
        if key in taken:                      # 同一个值的另一种措辞，不再占格
            continue
        if len(out) >= ANCHOR_MAX_LINES or used + len(line) > ANCHOR_MAX_CHARS:
            break
        taken.add(key)
        out.append(line)
        used += len(line)
    return out


def missing_anchors(summary_text, pinned):
    """确定性保真检查：哪些锚定行的关键片段没被摘要复述。

    **刻意不再调一次模型**。反馈书建议「压缩后让模型核对关键约束是否还在」，
    但那样每次压缩多一次 provider 往返，而且让模型自查自己的摘要恰恰是最不可靠
    的一环。这件事可以用字符串包含精确判定 —— 能确定性判定的就不要花钱去猜。
    判据取行内被模式命中的那一小段（如 `<1%`），而不是整行：摘要本来就会改写措辞。
    """
    text = str(summary_text or "")
    missing = []
    for line in pinned or []:
        needles = []
        for pattern in ANCHOR_PATTERNS:
            found = pattern.search(line)
            if found:
                needles.append(found.group(0).strip())
        if needles and not any(n and n in text for n in needles):
            missing.append(line)
    return missing


def make_summary(content, plan, *, model, gateway):
    return {
        "working_set": plan_working_set(plan),
        # 旧摘要没有这个键，读侧一律 .get(...) —— validate_summary 不枚举字段，
        # 所以加字段不必升 SUMMARY_VERSION，旧会话的摘要照样通过校验。
        "pinned": plan_anchor_lines(plan),
        "version": SUMMARY_VERSION,
        "status": "valid",
        "content": str(content).strip(),
        "covered_from": plan["covered_from"],
        "covered_to": plan["covered_to"],
        "covered_sha256": plan["covered_sha256"],
        "model": model,
        "gateway": gateway,
        "estimated_tokens": estimate_tokens(content),
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
