"""思考流：两种来源，一个缓冲，绝不进正文（SPEC-CC-parity D1）。

provider 交付"思考"有两种形状：
  1. 单独字段 reasoning_content（kimi-k3）—— client 已转成 {"t": "reasoning"} 事件；
  2. 塞在正文里的 <think>…</think>（minimax、部分 glm 版本）—— 从正文流里裸流过来。
Claude Code 的契约是：思考折叠成一行、可展开、永远不混进 assistant 正文。
这个模块只做两件纯函数式的事：把第二种从正文里**分离**出来（ThinkFilter），
把两种来源**累计**成一个可折叠块（ThinkingTracker）。不碰终端、不碰 provider。
"""
from __future__ import annotations

OPEN = "<think>"
CLOSE = "</think>"
MAX_KEEP = 200_000          # 累计保留的思考字符上限；超出只计数不保存


class ThinkFilter:
    """把正文流开头的 <think>…</think> 分离成思考。

    有状态：标签可能被 chunk 切在任意位置（"<th" + "ink>"），所以未定型时先攒。
    只认**消息开头**（忽略前导空白）的 <think> —— 正文中间出现的字面 "<think>"
    （模型在讨论这个标签本身）不算思考，避免误吞正文。未闭合（模型被截断）时
    finish() 把剩余全部当思考，不会漏到正文里。
    """

    def __init__(self):
        self._pending = ""
        self._state = "undecided"      # undecided | think | visible

    def feed(self, chunk):
        """喂一段正文；返回 (可见正文增量, 思考增量)。"""
        visible, thinking = "", ""
        self._pending += str(chunk or "")
        while self._pending:
            if self._state == "undecided":
                head = self._pending.lstrip()
                if not head:
                    break                              # 只有空白，还不能定
                if head.startswith(OPEN):
                    self._state = "think"
                    self._pending = head[len(OPEN):]
                    continue
                if OPEN.startswith(head):
                    break                              # 还是 "<think>" 的前缀，等
                self._state = "visible"
                continue
            if self._state == "think":
                idx = self._pending.find(CLOSE)
                if idx >= 0:
                    thinking += self._pending[:idx]
                    self._pending = self._pending[idx + len(CLOSE):]
                    self._state = "visible"
                    continue
                # 留一个可能被切开的 "</think>" 尾巴，其余先放出去
                keep = len(CLOSE) - 1
                if len(self._pending) > keep:
                    thinking += self._pending[:-keep]
                    self._pending = self._pending[-keep:]
                break
            # visible
            visible += self._pending
            self._pending = ""
        return visible, thinking

    def finish(self):
        """流结束：未定型的前缀是正文，未闭合的思考仍是思考。"""
        rest, self._pending = self._pending, ""
        if self._state == "think":
            return "", rest
        self._state = "visible"
        return rest, ""


class ThinkingTracker:
    """累计一个 turn 里的思考文本，给折叠行与 /expand think 用。"""

    def __init__(self):
        self.chars = 0
        self.parts = []
        self._kept = 0
        self.emitted = False          # 折叠行是否已写进 transcript

    def add(self, text):
        if not text:
            return
        self.chars += len(text)
        if self._kept < MAX_KEEP:
            take = text[:MAX_KEEP - self._kept]
            self.parts.append(take)
            self._kept += len(take)

    @property
    def text(self):
        return "".join(self.parts)

    def __bool__(self):
        return self.chars > 0

    def summary(self):
        n = self.chars
        size = f"{n/1000:.1f}k" if n >= 1000 else str(n)
        return f"✻ 思考 ({size} 字 · /expand think 展开)"

    def activity(self):
        n = self.chars
        size = f"{n/1000:.1f}k" if n >= 1000 else str(n)
        return f"✻ 思考中 · {size} 字"
