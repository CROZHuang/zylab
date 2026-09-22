"""child 的额外能力是**继承**来的，不是授予的（用户 2026-09-22 定：child 要能联网）。

派出去做网页调研的 child 只会回「我没有 web_fetch」（功能测试报告 §2），
而联网调研恰恰是子代理最该承担的活：大范围抓取 → 有界报告。

但不能白送。`web_fetch` 的权限默认是 `ask`——它是模型唯一的网络出口——而 child 的
`ag.confirm` 是硬编码 `True`、又拿不到 `decision_gate`。无条件把它加进
`CHILD_TOOLS` 等于让 child **无声地绕过那道 ask**。

所以判据是「父会话对 web_fetch 已经是允许的」：配置 allow / 本次会话按过
「总是允许」/ yolo。三者都是用户已经对这一会话表过态，child 继承它不新增同意面、
也不构成提权。

两道闸，缺一不可：
1. **会话层**决定给什么（只有它知道 grant 状态）；
2. **child 运行时**按 `CHILD_EXTRA_ALLOWED` 再过滤一遍——即使上层传错，
   也只有白名单里的能生效。
"""
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = str(Path(__file__).resolve().parents[1])
sys.path.insert(0, ROOT)

import tests  # noqa: F401,E402  —— 状态目录隔离

import zylab  # noqa: E402
from core import agents, settings, tools  # noqa: E402


class TheRuntimeFilter(unittest.TestCase):
    """第二道闸：child 运行时只认白名单。"""

    def tools_for(self, **record):
        return set(agents.child_tools(dict(kind="subagent", **record)))

    def test_without_anything_extra_it_is_read_only(self):
        self.assertEqual(self.tools_for(), set(agents.CHILD_TOOLS))
        self.assertNotIn("web_fetch", self.tools_for())

    def test_web_fetch_can_be_inherited(self):
        self.assertIn("web_fetch", self.tools_for(extra_tools=["web_fetch"]))

    def test_anything_outside_the_allowlist_is_dropped(self):
        """上层传错也不该让 child 多出一个写口或命令口。"""
        got = self.tools_for(extra_tools=[
            "bash", "write_file", "edit_file", "subagent", "decision_gate",
            "workflow", "web_fetch"])
        self.assertEqual(got, set(agents.CHILD_TOOLS) | {"web_fetch"})

    def test_the_allowlist_is_exactly_one_thing(self):
        """扩这个集合是安全决定，不该顺手做——改它就得改这条断言。"""
        self.assertEqual(agents.CHILD_EXTRA_ALLOWED, frozenset({"web_fetch"}))

    def test_consult_is_untouched(self):
        self.assertEqual(
            set(agents.child_tools(
                {"kind": "consult", "extra_tools": ["web_fetch"]})),
            set(agents.CONSULT_TOOLS))

    def test_a_missing_or_broken_field_is_treated_as_nothing_extra(self):
        for value in (None, (), [], "web_fetch", 0):
            with self.subTest(value=value):
                got = set(agents.child_tools(
                    {"kind": "subagent", "extra_tools": value}))
                # 字符串会被逐字符拆开，但没有单字符工具名，所以仍然是空集
                self.assertEqual(got, set(agents.CHILD_TOOLS))
        self.assertEqual(set(agents.child_tools(None)),
                         set(agents.CHILD_TOOLS))


class TheSessionDecides(unittest.TestCase):
    """第一道闸：只有会话知道本次会话授权了什么。"""

    def session(self, *, perm="ask", always=(), auto=False):
        sess = object.__new__(zylab.Session)
        cfg = dict(settings.DEFAULTS)
        cfg["permissions"] = dict(cfg["permissions"])
        cfg["permissions"]["web_fetch"] = perm
        sess.cfg = cfg
        sess.always = set(always)
        sess.auto = auto
        return sess

    def test_the_default_gives_the_child_nothing(self):
        """出厂 web_fetch 是 ask —— 没表态就没有网络出口。"""
        self.assertEqual(self.session().child_extra_tools(), ())

    def test_a_config_allow_is_inherited(self):
        self.assertEqual(
            self.session(perm="allow").child_extra_tools(), ("web_fetch",))

    def test_a_session_grant_is_inherited(self):
        """用户按过「总是允许」——那就是对这一会话表过态了。"""
        self.assertEqual(
            self.session(always={"web_fetch"}).child_extra_tools(),
            ("web_fetch",))

    def test_yolo_inherits_too(self):
        self.assertEqual(
            self.session(auto=True).child_extra_tools(), ("web_fetch",))

    def test_deny_beats_everything(self):
        """明确禁掉的，连 yolo 和 session grant 都不该越过。"""
        for kwargs in ({"auto": True}, {"always": {"web_fetch"}}):
            with self.subTest(**kwargs):
                sess = self.session(perm="deny", **kwargs)
                self.assertEqual(sess.child_extra_tools(), ())


class ThePlumbing(unittest.TestCase):
    def test_the_tool_layer_defaults_to_nothing_when_the_hook_is_absent(self):
        """某条路径没接上 hook 时，child 也不该多出网络出口。"""
        with mock.patch.dict(tools.HOOK_CTX, {}, clear=True):
            self.assertEqual(tools._child_extra_tools(), ())

    def test_a_raising_hook_is_not_fatal_and_grants_nothing(self):
        def boom():
            raise RuntimeError("坏了")
        with mock.patch.dict(tools.HOOK_CTX, {"child_extra_tools": boom}):
            self.assertEqual(tools._child_extra_tools(), ())

    def test_the_hook_value_reaches_the_tool_layer(self):
        with mock.patch.dict(
                tools.HOOK_CTX, {"child_extra_tools": lambda: ("web_fetch",)}):
            self.assertEqual(tools._child_extra_tools(), ("web_fetch",))

    def test_create_persists_only_allowlisted_extras(self):
        """落盘那一层也过滤——记录里不该出现 bash。"""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            store = agents.AgentStore(Path(tmp))
            ctx = tools.ExecutionContext.capture(
                model="m", gateway="g", interaction_role="child")
            record = store.create(
                task="查一下", context="", execution_context=ctx,
                extra_tools=["bash", "web_fetch", "write_file"])
        self.assertEqual(record["extra_tools"], ["web_fetch"])


class TheChildIsToldItCanBrowse(unittest.TestCase):
    def test_the_note_only_appears_when_the_tool_is_there(self):
        """不说它就不知道自己能上网；没有时也不能乱说。"""
        self.assertIn("web_fetch", agents.WEB_NOTE)
        source = (Path(ROOT) / "core" / "agents.py").read_text(
            encoding="utf-8")
        self.assertIn(
            'WEB_NOTE if "web_fetch" in child_tools(record) else ""', source,
            "提示词要按实际工具集条件追加，不能无条件贴上去")

    def test_the_note_says_fetched_pages_are_untrusted(self):
        """抓回来的网页是数据不是指令——这句得在提示词里。"""
        self.assertIn("不可信", agents.WEB_NOTE)


if __name__ == "__main__":
    unittest.main()
