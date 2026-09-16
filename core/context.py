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
from dataclasses import dataclass
from datetime import datetime, timezone

from . import attachments


SUMMARY_VERSION = 1
TOKEN_ESTIMATOR = "ascii-chars/4+non-ascii-chars+message-overhead"
RECENT_LARGE_TOOL_CHARS = 12_000
RECENT_KEEP_HEAD = 4_000
RECENT_KEEP_TAIL = 2_000
OLD_KEEP_TAIL = 200
# 工具**参数**也要老化：模型写文件时把整份内容放进 tool_calls.arguments，
# 实测一个真实会话里这些参数占了 17% 的上下文字符，而且永不老化 —— 工具结果被
# 收起后它们就成了最大的一块。内容早已落盘，历史里留个头就够。
ARG_MIN_CHARS = 600
ARG_KEEP_HEAD = 200


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
    if units is None:
        protocol_units, issues = build_units(messages, start)
        units = group_turns(protocol_units)
    else:
        _, issues = build_units(messages, start)
    boundaries = {start}
    boundaries.update(u.end for u in units)
    if covered_to not in boundaries:
        return False, "coverage_splits_conversation_turn"
    if any(issue["start"] < covered_to for issue in issues):
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
    digest = hashlib.sha256(content.encode("utf-8", "replace")).hexdigest()[:16]
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
    projected += "\n[原始结果仍保存在会话事件中；需要时重新读取或调用工具。]"
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


def _preview_arguments(arguments, name):
    """收起旧轮次里过大的工具参数；**必须保持 JSON 合法**，只换字符串值。"""
    if not isinstance(arguments, str) or len(arguments) < ARG_MIN_CHARS:
        return arguments, None
    try:
        parsed = json.loads(arguments)
    except (ValueError, TypeError):
        return arguments, None            # 参数不是合法 JSON：原样留着
    if not isinstance(parsed, dict):
        return arguments, None
    saved = 0
    for key, value in list(parsed.items()):
        if not isinstance(value, str) or len(value) < ARG_MIN_CHARS:
            continue
        digest = hashlib.sha256(
            value.encode("utf-8", "replace")).hexdigest()[:16]
        head = value[:ARG_KEEP_HEAD]
        marker = (
            head + f"\n…[参数投影 field={key} artifact=sha256:{digest} "
            f"original_chars={len(value):,} "
            f"omitted_chars={len(value) - len(head):,}"
            "；原始参数仍在会话事件里，落盘内容重新读文件即可]")
        if len(marker) >= len(value):
            continue                      # 投影不能增肥
        saved += len(value) - len(marker)
        parsed[key] = marker
    if not saved:
        return arguments, None
    projected = json.dumps(parsed, ensure_ascii=False)
    if len(projected) >= len(arguments):
        return arguments, None
    return projected, {
        "artifact": "args:sha256:" + hashlib.sha256(
            arguments.encode("utf-8", "replace")).hexdigest()[:16],
        "tool": name,
        "tier": "aged-arguments",
        "status": "ok-or-unknown",
        "original_chars": len(arguments),
        "projected_chars": len(projected),
        "saved_chars": max(0, len(arguments) - len(projected)),
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
            metadata = _call_metadata(messages[0])   # 先取原始 args 做表头
            recent = ranks.get(unit.start, age_after_turns + 1) <= age_after_turns
            if not recent:
                for call in messages[0].get("tool_calls") or []:
                    function = call.get("function") or {}
                    projected_args, preview = _preview_arguments(
                        function.get("arguments"),
                        str(function.get("name") or "tool"))
                    if preview:
                        function["arguments"] = projected_args
                        previews.append(preview)
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
    return [
        {"role": "assistant", "content": (
            f"<conversation-summary covers=\"raw:{summary['covered_from']}.."
            f"{summary['covered_to'] - 1}\">\n{summary['content']}\n"
            "</conversation-summary>" + working + "\n"
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

    omissions = _merge_budget_ranges([dict(item) for item in integrity])
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
    return {
        "covered_from": start,
        "covered_to": tail_start,
        "covered_sha256": _prefix_sha(raw, start, tail_start),
        "source_messages": source,
        "source_turns": len(covered),
        "source_message_count": len(source),
    }


def plan_compaction(messages, summary=None, keep_tail=6,
                    max_source_tokens=None):
    """选择完整轮次边界；返回生成摘要所需的原文，或 ``None``。

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
    remaining = [u for u in turns if u.start >= coverage]
    if keep_tail <= 0:
        # keep_tail=0 是 recap 用的：它不替换上下文，所以不需要给尾部留原文，
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
    if any(issue["start"] < tail_start for issue in issues):
        return None
    covered = [unit for unit in remaining if unit.end <= tail_start]
    if keep_tail <= 0 and covered:
        tail_start = covered[-1].end
    if not covered:
        return None
    if not max_source_tokens:
        return _assemble_plan(raw, start, active, covered, tail_start)

    # 二分找「渲染后仍不超预算」的最长前缀。渲染而不是估算原文，因为
    # summary_prompt 会把工具结果截到 600 字，两者能差一个数量级。
    def fits(count):
        plan = _assemble_plan(
            raw, start, active, covered[:count], covered[count - 1].end)
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
    if any(issue["start"] < covered_to for issue in issues):
        raise ValueError("summary cutoff 跨过不完整 tool call/result")
    boundaries = {unit.end for unit in turns}
    if covered_to not in boundaries:
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
    }


def summary_prompt(plan):
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
        # 标上轮次号，摘要才能给每条结论留下可回溯的锚点（recap 渲染成 ·t42）
        rows.append(f"[t{turn} {role}]\n{content}")
    # 会话在前、指令在后。指令放前面时模型会把「整理摘要」本身当成 objective
    # 写进去（2026-09-07 实测），因为那才是它看到的最后一条要求。
    return (
        "<会话前缀>\n" + "\n\n".join(rows) + "\n</会话前缀>\n\n"
        "上面是一段编码会话的前缀，它将被这份摘要替换掉，后续对话只能看到摘要。"
        "请整理成可续接的结构化交接摘要。\n"
        "- objective 写**会话里用户要达成的目标**，不是「整理摘要」这件事；"
        "本条指令不属于会话内容。\n"
        "- 只写有证据的事实，未知写 unknown，不要把省略或截断的内容补猜出来。\n"
        "- 标着 [summary] 的那条是更早一段的摘要：把它的内容合并进来，不要丢。\n"
        "- 工具结果在上面已被截断，引用它们时写清是哪个文件/命令，"
        "而不是复述截断的正文。\n"
        "- decisions **最新的写在最前面**，每条尽量带来源锚点 `[t<轮次>]`，"
        "轮次号取自上面每段开头的 `[t12 user]`。长会话里早期结论常被后来推翻，"
        "顺序错了会让接手的人拿着过时结论干活。\n"
        "- **被否决的方案也要写进 decisions**，格式 `[rejected] <方案> —— <原因>`。"
        "这类最不能丢：不记否决，接手的人会重新提案已经被否掉的东西。\n"
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
    items = list(seen.items())[-WORKING_SET_LIMIT:]
    return [{"path": path, "verb": verb} for path, verb in items]


def make_summary(content, plan, *, model, gateway):
    return {
        "working_set": working_set(plan.get("source_messages")),
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
