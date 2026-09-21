"""轮末自动记忆抽取：让模型自己把这次会话里值得长期留下的东西写成条目。

**为什么不指望主模型顺手写。** `memory_write` 工具一直都在、系统提示里也写清了该记什么，
2026-09-20 统计用户的 7 个真实会话：2,925 条助手消息里调用 **0 次**。同一天读 Claude Code /
Codex / ZCode 的实现，四家没有一家靠主模型自觉——都是另起一个抽取通道（CC 的
`extractMemories` 是轮末分叉、与父共用缓存的 agent）。

**机制与 recap 同源**（`away_recap`）：上下文 = 主请求那份投影 + 末尾追加下面这段提示，
前缀逐字节一致好吃 prompt cache，工具照发但不许用。实测（deepseek-v4-flash@deepinfer，
4 个真实会话）：3.5–24 s、约 700 输出 token，写出来的是「Triton 练习环境在哪、无 GPU 怎么
验证、ptxas 不在 PATH、跨 program 归约会互相覆盖（实测 err 29.4）」这种东西——对照旧的
确定性 handoff 写出来的「用户目标：好的」。

**输出一律当成可能残缺的**：同一次实测里 glm-5.3 把它写成了散文并撞满额度、
deepseek 有一次在最后一个 `}` 之前停住。所以解析是「逐个对象抢救」，救回几条算几条；
一条都救不回来就安静地什么都不写——记忆错了比没有更糟。
"""
from __future__ import annotations

import json
import re

from . import memory as memory_db

PROMPT = """把这次会话里**值得跨会话长期保留**的东西写下来。这是给未来的你看的笔记，不是总结。

该记（每条必须能独立看懂）：
- 用户稳定的偏好、约束、规矩，以及**为什么**；
- 实测出来的数字与结论，带**怎么测的**；
- 考虑过但**明确否决**的方案，以及否决的理由；
- 外部资源的指针（路径、URL、工单），但绝不含凭据本身。

不该记：代码结构、git 历史、读一下文件就能知道的事实；只在这次对话里有意义的中间状态
（谁在跑、进度到哪、某个进程已经 kill）；没有依据的猜测；这次会话的流水账。

写法：一条一事；相对日期换成绝对日期；写清「为什么」和「以后怎么用」，不只写结论。
`description` 一行，必须包含用户以后会用来找它的词（中文任务就写中文）。

只输出 JSON，不要任何别的文字：
{"entries": [{"name": "kebab-case-短名", "type": "user|feedback|project|reference",
  "description": "一行，≤150 字", "content": "完整事实；为什么；以后怎么用",
  "updates": "要更新的已有条目 id，没有就 null"}]}

没有任何东西值得记就输出 {"entries": []} —— 这是常见且正确的答案。
直接用文本回答这一个 JSON，不要调用任何工具。"""

MAX_TOKENS = 3_000             # 实测一次抽取 336–1,047 输出 token
RETRY_MAX_TOKENS = 8_000       # 关不掉思考的模型要给思考和正文都留下地方
MAX_ENTRIES = 5                # 一次最多落 5 条；再多多半是在记流水账
MIN_USER_MESSAGES = 2          # 一问一答还不值得抽
MIN_NEW_USER_MESSAGES = 2      # 距上次抽取新增的真实提问
IDLE_SECONDS = 60.0            # 一轮结束后静置这么久才抽
NAME = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
STABLE_PREFIX = "auto:"


def existing_block(index_text):
    """把已有条目的钩子索引附在提示后面，让模型去更新而不是造近似副本。"""
    text = str(index_text or "").strip()
    if not text:
        return ""
    return ("\n\n已有条目（同一件事要更新——把它的 id 填进 updates，不要新建近似副本）：\n"
            + text)


def salvage(text):
    """从可能被截断、被 ``` 包住、或多写了一层的输出里捞出完整的条目对象。

    实测过的两种残缺：整段是合法 JSON 但少了最后一个 `}`（deepseek）；根本没写 JSON
    而是散文（glm-5.3，撞满额度）。逐个对象解析能救回前者的 n−1 条，后者返回空。
    """
    body = str(text or "").strip()
    body = re.sub(r"^```(?:json)?\s*|\s*```$", "", body, flags=re.MULTILINE).strip()
    decoder = json.JSONDecoder()
    found, index = [], 0
    while index < len(body):
        start = body.find("{", index)
        if start < 0:
            break
        try:
            value, end = decoder.raw_decode(body, start)
        except ValueError:
            index = start + 1
            continue
        index = end
        if not isinstance(value, dict):
            continue
        if isinstance(value.get("entries"), list):
            found.extend(item for item in value["entries"]
                         if isinstance(item, dict))
        elif value.get("content"):
            found.append(value)
    return found


def clean(entries, *, cap=MAX_ENTRIES):
    """只留结构完整、名字规范、内容够实的条目；同名去重。"""
    out, seen = [], set()
    for item in entries or ():
        name = " ".join(str(item.get("name") or "").split()).lower()
        content = str(item.get("content") or "").strip()
        if not NAME.fullmatch(name) or len(content) < 40:
            continue
        if name in seen:
            continue
        seen.add(name)
        kind = str(item.get("type") or "").strip().lower()
        updates = str(item.get("updates") or "").strip()
        out.append({
            "name": name,
            "type": kind if kind in memory_db.TYPES else memory_db.DEFAULT_TYPE,
            "description": " ".join(str(item.get("description") or "").split()),
            "content": content[:memory_db.MAX_ENTRY_CHARS],
            "updates": updates if updates and updates.lower() != "null" else "",
            "stable_key": STABLE_PREFIX + name,
        })
        if len(out) >= cap:
            break
    return out


def parse(text, *, cap=MAX_ENTRIES):
    return clean(salvage(text), cap=cap)
