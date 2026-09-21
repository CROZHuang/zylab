"""离开期间的会话 recap —— 行为与机制照搬 Claude Code 的 awaySummary。

依据是 CC 2.1.272 二进制里的明文实现（不是印象）：

- **是模型现写的一两句话**，不是从落盘数据拼出来的多行结构块。旧的零调用 recap
  应付不了开放式的会话（09-20 用户原话）。
- **在你离开期间生成好等你**，不是回来时才生成：订阅终端焦点事件，失焦后装定时器，
  到点仍失焦且没有轮次在跑 → 后台生成；重新聚焦即取消定时器并中止在途生成。
- 生成 = 主模型 + 与主请求**逐字节相同的前缀**（同 system、同工具定义）+ 末尾追加下面这句
  提示词；工具照发但不许用（为了共用 prompt cache）、单轮；
  结果截到 400 字符。存成只给人看的记录，**不进发给模型的 messages**。
- 一串「不打扰」的闸：输入框有草稿 / 有后台工作 / 已在生成 / 本轮连续失败 3 次 /
  真实提问不足 3 条 / 距上次 recap 新增提问不足 2 条 / 生成完发现新一轮已开始 → 丢弃。

两处刻意的偏离：CC 只在 prompt cache 还热时才生成（缓存年龄未知则跳过）——这里的
网关不报告缓存 TTL，照搬等于永不触发，故去掉这道闸；没见过失焦事件（终端不支持
1004）时永不自动触发，这一点与 CC 一致，`/recap` 仍可用。
"""
from __future__ import annotations

import re
import threading
import time

from . import goals

# CC 原文，一字未动；后面两句是补的——"under 40 words" 对中文没有约束力；工具定义为了
# 共用 prompt cache 照发给模型（CC 是在权限层拒绝），所以得明说别用。
PROMPT = (
    "The user stepped away and is coming back. Recap in under 40 words, 1-2 plain "
    "sentences, no markdown. Lead with the overall goal and current task, then the "
    "one next action. Skip root-cause narrative, fix internals, secondary to-dos, "
    "and em-dash tangents."
    " Reply in the language the user has been writing in; for Chinese, stay under "
    "80 characters. Answer directly in text; do not call any tools.")

CHAR_CAP = 400                 # CC: m=400，按字符截
DELAY_SECONDS = 300.0          # CC 面向用户的说法：离开 5 分钟以上
MIN_DELAY_SECONDS = 30.0       # CC: G5o
BLUR_DEBOUNCE_SECONDS = 2.0    # CC: Q5o —— 切一下窗口马上回来不算离开
MIN_USER_MESSAGES = 3          # CC: J5o
MIN_NEW_USER_MESSAGES = 2      # CC: X5o —— 防止反复总结同一段
MAX_FAILURES_PER_TURN = 3      # CC: Y5o
HINT_FIRST_N = 3               # CC: Z5o —— 前几次附一句怎么关

_THINK = re.compile(r"<think>.*?</think>", re.S | re.I)
_DANGLING_THINK = re.compile(r"<think>.*\Z", re.S | re.I)


def _text_of(content):
    """user content 可能是带附件的分段列表；只取其中的文字。"""
    if isinstance(content, str):
        return content
    if isinstance(content, (list, tuple)):
        return "\n".join(
            str(part.get("text") or "") for part in content
            if isinstance(part, dict) and part.get("type") == "text")
    return ""


def real_user_messages(messages):
    """用户真正敲下的提问数。不算运行时合成的 user 行：`[…]` 开头的通知、
    goal 自动续轮的 `<goal_round>` 内部提示——那些不是人说的话，不能拿来凑
    「够 3 条才 recap」的数（对应 CC 里排除 meta / synthetic 消息）。"""
    count = 0
    for message in messages or ():
        if not isinstance(message, dict) or message.get("role") != "user":
            continue
        text = _text_of(message.get("content")).strip()
        if not text or text.startswith("[") or goals.is_round_prompt(text):
            continue
        count += 1
    return count


def eligible(messages, last_recap_users=None):
    """够不够格做一次 recap（CC 的 eJo）。便宜的判断，不发请求。"""
    users = real_user_messages(messages)
    if users < MIN_USER_MESSAGES:
        return False
    if last_recap_users is None or int(last_recap_users) > users:
        return True                   # 后者：历史被 rewind 截短了，旧计数已无意义
    return users - int(last_recap_users) >= MIN_NEW_USER_MESSAGES


def clean(text):
    """把模型输出整形成一行：去思考、去换行、截到 CHAR_CAP。返回 (text, capped)。"""
    text = _THINK.sub("", str(text or ""))
    text = _DANGLING_THINK.sub("", text)          # 思考没闭合就被截断的残段
    text = " ".join(text.split()).strip()
    if len(text) <= CHAR_CAP:
        return text, False
    return text[:CHAR_CAP - 1].rstrip() + "…", True


class AwayRecapController:
    """CC 的 U3e：何时生成、何时不打扰。与 TUI、agent 都解耦，时钟与定时器可注入。

    generate(cancel_event) -> {"kind": "ok"|…, "text": str}   在后台线程里被调用
    state() -> {"loading", "draft", "background", "messages", "last_recap_users"}
    deliver(text, raw) -> None   只入队；渲染与落盘由主循环做（后台线程不写屏）
    """

    def __init__(self, *, generate, state, deliver, enabled=True,
                 delay=DELAY_SECONDS, clock=time.monotonic,
                 timer=threading.Timer, hint="", cancel_factory=threading.Event):
        self._generate = generate
        # 中止句柄只需要 set()/is_set()。真实会话传 client.CancellationHandle：
        # 它的 set() 会 shutdown socket——读线程多半正阻塞在等首字节（慢网关上
        # 一两分钟），光置位一个 Event 叫不醒它。
        self._cancel_factory = cancel_factory
        self._state = state
        self._deliver = deliver
        self._enabled = bool(enabled)
        self._delay = max(MIN_DELAY_SECONDS, float(delay))
        self._clock = clock
        self._timer_factory = timer
        self._hint = str(hint or "")
        self._lock = threading.RLock()
        self._loading = False
        self._turn_end = None          # 上一轮结束的时刻；没有完整的一轮就没什么可 recap
        self._focused = None           # None = 从没见过焦点事件（终端不支持）
        self._first_blur = None
        self._timer = None
        self._inflight = None
        self._failures = 0
        self._shown = 0
        self.last_skip = None          # 诊断用：上一次为什么没生成

    # ---- 输入 -----------------------------------------------------------
    def set_enabled(self, enabled):
        with self._lock:
            self._enabled = bool(enabled)
            if not self._enabled:
                self._cancel_timer()
                self._abort()
            else:
                self._arm()

    def turn_started(self):
        with self._lock:
            self._loading = True
            self._cancel_timer()
            self._abort()              # 新一轮开始，在途的 recap 已经过时

    def turn_ended(self):
        with self._lock:
            self._loading = False
            self._turn_end = self._clock()
            self._failures = 0
            self._arm()

    def focus_changed(self, focused):
        with self._lock:
            self._focused = bool(focused)
            if focused:
                self._first_blur = None
                self._cancel_timer()
                self._abort()          # 人回来了：不再需要，也别让它事后冒出来
                return
            if self._first_blur is None:
                self._first_blur = self._clock()
            self._arm()

    def request(self):
        """resume 之后主动要一条，不等失焦。刚接上的会话在本进程里还没有「完整的
        一轮」，所以不看那道闸；其余「不打扰」的闸照旧。后台线程，立刻返回。"""
        with self._lock:
            if not self._enabled:
                return None
        worker = threading.Thread(
            target=self.maybe_generate, kwargs={"require_turn": False},
            name="away-recap", daemon=True)
        worker.start()
        return worker

    def dispose(self):
        with self._lock:
            self._enabled = False
            self._cancel_timer()
            self._abort()

    # ---- 定时 -----------------------------------------------------------
    def _cancel_timer(self):
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None

    def _abort(self):
        if self._inflight is not None:
            self._inflight.set()
            self._inflight = None

    def _arm(self):
        self._cancel_timer()
        if not self._enabled or self._loading or self._turn_end is None:
            return
        now = self._clock()
        debounce = (0.0 if self._first_blur is None
                    else BLUR_DEBOUNCE_SECONDS - (now - self._first_blur))
        wait = max(0.0, self._delay - (now - self._turn_end), debounce)
        timer = self._timer_factory(wait, self._fire)
        try:
            timer.daemon = True
        except AttributeError:
            pass
        self._timer = timer
        timer.start()

    def _fire(self):
        with self._lock:
            self._timer = None
            # 只有**明确看到**失焦才算离开；None（不支持焦点事件）永不自动触发。
            if self._focused is not False or self._loading:
                return
        self.maybe_generate()

    # ---- 生成 -----------------------------------------------------------
    def _skip(self, reason):
        self.last_skip = reason
        return None

    def maybe_generate(self, require_turn=True):
        with self._lock:
            if not self._enabled:
                return self._skip("disabled")
            if require_turn and self._turn_end is None:
                return self._skip("no completed turn")
            if self._inflight is not None:
                return self._skip("generation in flight")
            if self._failures >= MAX_FAILURES_PER_TURN:
                return self._skip("failing repeatedly this turn")
            state = dict(self._state() or {})
            if state.get("loading"):
                return self._skip("turn running")
            if state.get("draft"):
                return self._skip("draft input present")
            if state.get("background"):
                return self._skip("background work pending")
            if not eligible(state.get("messages"), state.get("last_recap_users")):
                return self._skip("not enough new conversation")
            cancel = self._cancel_factory()
            self._inflight = cancel
        try:
            result = dict(self._generate(cancel) or {})
        except Exception as exc:                      # noqa: BLE001 - recap 不能弄崩会话
            result = {"kind": "failed", "text": f"{type(exc).__name__}: {exc}"}
        finally:
            with self._lock:
                if self._inflight is cancel:
                    self._inflight = None
        if cancel.is_set():
            return self._skip("aborted")
        if result.get("kind") != "ok" or not str(result.get("text") or "").strip():
            with self._lock:
                self._failures += 1
            return self._skip("generate failed: " + str(result.get("kind")))
        if dict(self._state() or {}).get("loading"):
            return self._skip("dropped: new turn already running")
        raw = str(result["text"])
        with self._lock:
            text = raw + (self._hint if self._shown < HINT_FIRST_N else "")
            self._shown += 1
        self.last_skip = None
        self._deliver(text, raw)
        return text
