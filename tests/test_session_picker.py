"""M4a session picker state-machine and transactional resume tests."""
import copy
import io
import os
import sys
import tempfile
import unittest
from contextlib import contextmanager


@contextmanager
def chdir(path):
    """`contextlib.chdir` 是 3.11 才有的，而 README 承诺 3.10+。

    2026-09-21 公开仓库第一次 CI，`py3.10` 那一列这个模块直接 import 不了
    （`ImportError: cannot import name 'chdir'`）。产品代码没用到它，但测试
    套件在最低版本上跑不起来，等于最低版本根本没被验证过。
    """
    import os
    before = os.getcwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(before)
from pathlib import Path
from unittest import mock


sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import tui
import zylab as CLI


class SessionPickerInputTests(unittest.TestCase):
    def test_ctrl_a_is_distinct_key_and_line_editor_keeps_home_semantics(self):
        self.assertEqual(tui.KEYS["\x01"], "ctrl-a")
        self.assertNotEqual(tui.KEYS["\x01"], "home")
        self.assertEqual(tui.KEYS["\x1b[H"], "home")

        for key in ("ctrl-a", "home"):
            with self.subTest(key=key):
                editor = tui.LineEditor()
                editor.replace("abc")
                editor.handle("left")

                event = editor.handle(key)[0]

                self.assertEqual(event.kind, "redraw")
                self.assertEqual(editor.text, "abc")
                self.assertEqual(editor.cursor, 0)

    def test_explicit_filter_makes_lowercase_actions_searchable(self):
        rows = ["ruby", "alpha", "foxy"]
        pump = tui.InputPump(stream=io.StringIO())
        pump.open_picker(
            rows,
            explicit_filter=True,
            actions={"R": "rename", "A": "archive", "F": "fork"},
        )

        entered = pump._handle_picker("/")[0]
        self.assertEqual(entered.kind, "redraw")
        self.assertTrue(pump._picker["filter_mode"])

        for key, expected in (("r", "ruby"),
                              ("a", "alpha"),
                              ("f", "foxy")):
            with self.subTest(key=key):
                pump._handle_picker("ctrl-u")
                event = pump._handle_picker(key)[0]
                self.assertEqual(event.kind, "redraw")
                self.assertEqual(pump._mode, "picker")
                self.assertEqual(pump._picker["filter"], key)
                self.assertEqual(event.snapshot.options, (expected,))

    def test_uppercase_actions_fire_only_in_browse_mode(self):
        for key, action in (("R", "rename"),
                            ("A", "archive"),
                            ("F", "fork")):
            with self.subTest(key=key):
                pump = tui.InputPump(stream=io.StringIO())
                pump.open_picker(
                    ["row"],
                    explicit_filter=True,
                    actions={key: action},
                )
                pump._handle_picker("/")

                filtered = pump._handle_picker(key)[0]

                self.assertEqual(filtered.kind, "redraw")
                self.assertEqual(pump._mode, "picker")
                pump._handle_picker("ctrl-u")
                pump._handle_picker("esc")

                selected = pump._handle_picker(key)[0]

                self.assertEqual(
                    (selected.kind, selected.value, selected.mode),
                    ("picker_result", "row", action),
                )

    def test_escape_leaves_filter_then_closes_picker(self):
        pump = tui.InputPump(stream=io.StringIO())
        pump.open_picker(["row"], explicit_filter=True)
        pump._handle_picker("/")
        pump._handle_picker("r")

        leave_filter = pump._handle_picker("esc")

        self.assertEqual([event.kind for event in leave_filter], ["redraw"])
        self.assertEqual(pump._mode, "picker")
        self.assertFalse(pump._picker["filter_mode"])
        self.assertEqual(pump._picker["filter"], "r")

        close = pump._handle_picker("esc")

        self.assertEqual(
            [event.kind for event in close],
            ["picker_result", "redraw"],
        )
        self.assertIsNone(close[0].value)
        self.assertEqual(pump._mode, "line")

    def test_allow_empty_picker_still_emits_ctrl_a_scope_action(self):
        pump = tui.InputPump(stream=io.StringIO())
        pump.open_picker(
            [],
            allow_empty=True,
            explicit_filter=True,
            actions={"ctrl-a": "toggle-scope"},
        )

        event = pump._handle_picker("ctrl-a")[0]

        self.assertEqual(
            (event.kind, event.value, event.mode),
            ("picker_result", None, "toggle-scope"),
        )

    def test_query_and_selected_key_survive_picker_close_and_reopen(self):
        rows = [
            {"id": "one", "title": "same one"},
            {"id": "two", "title": "same two"},
            {"id": "other", "title": "different"},
        ]
        options = {
            "render": lambda row: row["title"],
            "filter_text": lambda row: row["title"],
            "row_key": lambda row: row["id"],
            "explicit_filter": True,
            "actions": {"space": "preview"},
        }
        pump = tui.InputPump(stream=io.StringIO())
        pump.open_picker(rows, **options)
        pump._handle_picker("/")
        for char in "same":
            pump._handle_picker(char)
        pump._handle_picker("down")
        pump._handle_picker("esc")

        closed = pump._handle_picker(" ")[0]

        self.assertEqual(closed.value["id"], "two")
        self.assertEqual(closed.state["filter"], "same")
        self.assertEqual(closed.state["selected_key"], "two")

        pump.open_picker(
            [rows[1], rows[0], rows[2]],
            initial_state=closed.state,
            **options,
        )
        reopened = pump._picker_state()

        self.assertEqual(reopened["filter"], "same")
        self.assertEqual(reopened["selected_key"], "two")

    def test_prompt_modal_preserves_main_draft_on_submit_and_cancel(self):
        for close_key, expected in (("enter", "rename old!"),
                                    ("esc", None)):
            with self.subTest(close_key=close_key):
                pump = tui.InputPump(stream=io.StringIO())
                pump.editor.replace("main draft")
                pump.open_prompt(
                    prompt="Rename > ", initial="rename old", title="Rename")
                pump._handle_prompt("!")

                result = pump._handle_prompt(close_key)[0]

                self.assertEqual(result.kind, "prompt_result")
                self.assertEqual(result.value, expected)
                self.assertEqual(pump._mode, "line")
                self.assertEqual(pump.snapshot().text, "main draft")

    def test_picker_rows_and_metadata_stay_bounded_in_narrow_terminals(self):
        for columns in (20, 30):
            with self.subTest(columns=columns), mock.patch.object(
                    tui, "_cols", return_value=columns):
                stream = io.StringIO()
                renderer = tui.TerminalRenderer(stream)
                renderer.render(tui.InputSnapshot(
                    mode="picker",
                    title="S",
                    options=("很长的会话标题-with-a-long-suffix",),
                    selected=0,
                    controls="Esc",
                    status="model@gateway with a very long context status",
                ))

                plain = tui._ANSI.sub("", stream.getvalue()).replace("\r", "")
                lines = [line for line in plain.split("\n") if line]

                self.assertGreaterEqual(len(lines), 4)
                for line in lines:
                    self.assertLessEqual(
                        tui.display_width(line),
                        columns,
                        (columns, line, tui.display_width(line)),
                    )

    def test_picker_filter_state_is_not_hidden_by_long_controls(self):
        stream = io.StringIO()
        controls = (
            "↑↓ · Enter resume · Space preview · R rename · "
            "A archive · F fork · Tab archived · Ctrl+B branch · "
            "Ctrl+A all · Esc")
        with mock.patch.object(tui, "_cols", return_value=80):
            tui.TerminalRenderer(stream).render(tui.InputSnapshot(
                mode="picker",
                title="Resume session",
                options=("row",),
                selected=0,
                hint="1 项 · 筛选: raf · / 编辑",
                controls=controls,
                status="model@gateway · cwd",
            ))

        plain = tui._ANSI.sub("", stream.getvalue()).replace("\r", "")
        self.assertIn("筛选: raf", plain)
        for action in (
                "Enter resume", "Space preview", "R rename", "A archive",
                "F fork", "Tab archived", "Ctrl+B branch",
                "Ctrl+A all", "Esc"):
            with self.subTest(action=action):
                self.assertIn(action, plain)
        for line in (line for line in plain.splitlines() if line):
            self.assertLessEqual(tui.display_width(line), 80)

    def test_picker_page_budget_accounts_for_two_control_rows(self):
        controls = (
            "↑↓ · Enter resume · Space preview · R rename · "
            "A archive · F fork · Tab archived · Ctrl+B branch · "
            "Ctrl+A all · Esc")
        pump = tui.InputPump(stream=io.StringIO())
        with mock.patch.object(tui, "_cols", return_value=80), \
                mock.patch.object(tui, "_lines", return_value=10):
            pump.open_picker(
                [f"session-{index}" for index in range(20)],
                title="Resume session", page=12, controls=controls)
            snapshot = pump.snapshot()

        # 10 terminal rows - title/state/two controls/metadata = five options.
        self.assertEqual(len(snapshot.options), 5)


class FakeAgent:
    def __init__(self, session_id="current"):
        self.session_id = session_id
        self.model = "current-model"
        self.gateway = CLI.client.GATEWAY
        self.tokens_in = 3
        self.tokens_out = 2
        self.last_total = 5
        self.messages = [
            {"role": "system", "content": "current system"},
            {"role": "user", "content": "current question"},
        ]
        self.context = {"current": True}
        self.refresh_count = 0

    def context_snapshot(self):
        return dict(self.context)

    def load_context(self, context):
        self.context = context

    def set_model(self, model):
        self.model = model

    def refresh_environment(self):
        self.refresh_count += 1


class FakeResumeSession:
    def __init__(self, cwd):
        self.ag = FakeAgent()
        self.confirm = lambda _name, _args: True
        self.permission_decision = lambda _request: True
        self.last_ctx = self.ag.last_total
        self._title = "current title"
        self._session_cwd = str(cwd)
        self.plan_mode = False
        self.cfg = {"resume_replay": 2}
        self.renderer = None
        self.controller = ("controller", self.ag.session_id)
        self.lease_owner_id = "test-owner"
        self._leased_session_id = self.ag.session_id
        self.calls = []
        self.blocker = None
        self.acquire_error = None
        self.save_error = None
        self.commit_error = None

    def cwd_switch_blocker(self):
        self.calls.append(("blocker",))
        return self.blocker

    def acquire_target_lease(self, session_id):
        self.calls.append(("acquire", session_id))
        if self.acquire_error is not None:
            raise self.acquire_error
        return True

    def commit_target_lease(self, session_id):
        self.calls.append(("commit", session_id))
        if self.commit_error is not None:
            raise self.commit_error
        self._leased_session_id = session_id
        return True

    def rollback_target_lease(self, session_id):
        self.calls.append(("rollback", session_id))
        return True

    def save(self):
        self.calls.append(("save", self.ag.session_id))
        if self.save_error is not None:
            raise self.save_error

    def detach_task(self):
        self.calls.append(("detach",))

    def reset_agent_workspace(self):
        self.calls.append(("reset-agents",))

    def reset_controller(self):
        self.calls.append(("reset-controller", self.ag.session_id))
        self.controller = ("controller", self.ag.session_id)
        return self.controller


def _resume_record(cwd, session_id="target"):
    return {
        "id": session_id,
        "title": "target title",
        "cwd": str(cwd),
        "model": "target-model",
        "gateway": CLI.client.GATEWAY,
        "tokens_in": 17,
        "tokens_out": 9,
        "last_context_tokens": 26,
        "context": {"target": True},
        "messages": [
            {"role": "system", "content": "target system"},
            {"role": "user", "content": "target question"},
        ],
    }


@contextmanager
def _canonical(record, *, on_load=None):
    def load(session_id):
        if on_load is not None:
            on_load(session_id)
        return record

    with mock.patch.object(
            CLI.store, "resolve_session_id", return_value=record["id"]), \
            mock.patch.object(CLI.store, "load_session", side_effect=load):
        yield


class ResumeCwdTransactionTests(unittest.TestCase):
    def test_commit_failure_restores_cwd_health_policy_and_metrics_facade(self):
        with tempfile.TemporaryDirectory() as root:
            current = Path(root) / "current"
            saved = Path(root) / "saved"
            current.mkdir()
            saved.mkdir()
            sess = FakeResumeSession(current)
            sess.commit_error = OSError("commit failed")
            old_cfg = copy.deepcopy(CLI.CFG.DEFAULTS)
            old_cfg["model_health"]["probe_success_ttl"] = 111
            new_cfg = copy.deepcopy(CLI.CFG.DEFAULTS)
            new_cfg["model_health"]["probe_success_ttl"] = 222
            old_status = {"marker": "OLD", "network_marker": "NET OLD"}
            new_status = {"marker": "NEW", "network_marker": "NET NEW"}
            old_metrics = object()
            new_metrics = object()
            sess.cfg = old_cfg
            sess.cfg_sources = ["old-source"]
            sess._sandbox_status = old_status
            sess.auto = False
            sess._auto_override = None
            sess.ag.hook_cfg = old_cfg
            sess.ag.metrics = old_metrics
            sess.ag.compact_override = old_cfg["compact_at"]
            sess.ag.compact_at = 70_000
            sess.ag.ctx_limit = 100_000
            sess.configuration_snapshot = (
                CLI.Session.configuration_snapshot.__get__(sess))
            sess.apply_cwd_configuration = (
                CLI.Session.apply_cwd_configuration.__get__(sess))
            sess.restore_configuration_snapshot = (
                CLI.Session.restore_configuration_snapshot.__get__(sess))
            new_policy = CLI.CFG.model_health_policy(new_cfg)
            old_policy = CLI.CFG.model_health_policy(old_cfg)
            sess.prepare_cwd_configuration = lambda cwd: {
                "cfg": new_cfg,
                "sources": ["new-source"],
                "sandbox_status": new_status,
                "health_policy": new_policy,
            }
            record = _resume_record(saved)
            previous_hook_cfg = CLI.tools.HOOK_CTX.get("cfg")

            try:
                with (
                    chdir(current),
                    _canonical(record),
                    mock.patch.object(
                        CLI.store, "configure_metrics_policy") as configure,
                    mock.patch.object(
                        CLI.store, "metrics_facade",
                        side_effect=[new_metrics, old_metrics]),
                    self.assertRaisesRegex(OSError, "commit failed"),
                ):
                    CLI.resume_session_record(
                        sess, record,
                        cwd_policy="current", replay=False)
            finally:
                CLI.tools.HOOK_CTX["cfg"] = previous_hook_cfg

        self.assertIs(sess.cfg, old_cfg)
        self.assertEqual(sess.cfg_sources, ["old-source"])
        self.assertIs(sess._sandbox_status, old_status)
        self.assertIs(sess.ag.hook_cfg, old_cfg)
        self.assertIs(sess.ag.metrics, old_metrics)
        self.assertEqual(
            configure.call_args_list,
            [mock.call(new_policy), mock.call(old_policy)])
        self.assertIn(("rollback", "target"), sess.calls)

    def test_current_policy_rebinds_without_chdir_and_commits_lease(self):
        with tempfile.TemporaryDirectory() as root:
            current = Path(root) / "current"
            saved = Path(root) / "saved"
            current.mkdir()
            saved.mkdir()
            sess = FakeResumeSession(current)
            record = _resume_record(saved)

            with chdir(current), _canonical(record), mock.patch.object(
                    CLI.store, "update_session_metadata") as update, \
                    mock.patch.object(CLI, "_session_notice"):
                resumed = CLI.resume_session_record(
                    sess, record,
                    cwd_policy="current", replay=False)

                self.assertTrue(resumed)
                self.assertEqual(Path.cwd(), current)

            self.assertEqual(sess.ag.session_id, "target")
            self.assertEqual(sess._session_cwd, str(current))
            self.assertIn(("acquire", "target"), sess.calls)
            self.assertIn(("commit", "target"), sess.calls)
            self.assertNotIn(("rollback", "target"), sess.calls)
            update.assert_called_once_with(
                "target", owner_id="test-owner", cwd=str(current))

    def test_saved_policy_chdirs_after_runtime_check_and_commits_lease(self):
        with tempfile.TemporaryDirectory() as root:
            current = Path(root) / "current"
            saved = Path(root) / "saved"
            current.mkdir()
            saved.mkdir()
            sess = FakeResumeSession(current)
            record = _resume_record(saved)

            with chdir(current), _canonical(record), mock.patch.object(
                    CLI.store, "update_session_metadata") as update, \
                    mock.patch.object(CLI, "_session_notice"):
                resumed = CLI.resume_session_record(
                    sess, record,
                    cwd_policy="saved", replay=False)

                self.assertTrue(resumed)
                self.assertEqual(Path.cwd(), saved)

            self.assertEqual(sess.ag.session_id, "target")
            self.assertEqual(sess._session_cwd, str(saved))
            self.assertLess(
                sess.calls.index(("acquire", "target")),
                sess.calls.index(("blocker",)),
            )
            self.assertIn(("commit", "target"), sess.calls)
            self.assertNotIn(("rollback", "target"), sess.calls)
            update.assert_not_called()

    def test_cancel_policy_has_no_session_or_lease_side_effects(self):
        with tempfile.TemporaryDirectory() as root:
            current = Path(root) / "current"
            saved = Path(root) / "saved"
            current.mkdir()
            saved.mkdir()
            sess = FakeResumeSession(current)
            record = _resume_record(saved)

            with chdir(current), _canonical(record), \
                    mock.patch.object(CLI, "_session_notice"):
                resumed = CLI.resume_session_record(
                    sess, record,
                    cwd_policy="cancel", replay=False)

            self.assertFalse(resumed)
            self.assertEqual(sess.ag.session_id, "current")
            self.assertEqual(sess.calls, [
                ("acquire", "target"),
                ("rollback", "target"),
            ])
            self.assertEqual(sess._leased_session_id, "current")

    def test_saved_policy_missing_directory_releases_target_lease(self):
        with tempfile.TemporaryDirectory() as root:
            current = Path(root) / "current"
            current.mkdir()
            missing = Path(root) / "missing"
            sess = FakeResumeSession(current)
            record = _resume_record(missing)

            with chdir(current), _canonical(record), \
                    self.assertRaises(FileNotFoundError):
                CLI.resume_session_record(
                    sess, record,
                    cwd_policy="saved", replay=False)

            self.assertEqual(sess.calls, [
                ("acquire", "target"),
                ("rollback", "target"),
            ])
            self.assertEqual(sess.ag.session_id, "current")

    def test_saved_policy_active_runtime_releases_target_lease(self):
        with tempfile.TemporaryDirectory() as root:
            current = Path(root) / "current"
            saved = Path(root) / "saved"
            current.mkdir()
            saved.mkdir()
            sess = FakeResumeSession(current)
            sess.blocker = "1 个 managed task 仍在运行"
            record = _resume_record(saved)

            with chdir(current), _canonical(record), self.assertRaisesRegex(
                    RuntimeError, "managed task"):
                CLI.resume_session_record(
                    sess, record,
                    cwd_policy="saved", replay=False)

            self.assertEqual(sess.calls, [
                ("acquire", "target"),
                ("blocker",),
                ("rollback", "target"),
            ])
            self.assertEqual(sess.ag.session_id, "current")

    def test_live_target_lease_blocks_before_current_session_is_saved(self):
        with tempfile.TemporaryDirectory() as root:
            current = Path(root) / "current"
            saved = Path(root) / "saved"
            current.mkdir()
            saved.mkdir()
            sess = FakeResumeSession(current)
            sess.acquire_error = CLI.store.SessionBusyError(
                "target", {
                    "owner_id": "other-owner",
                    "pid": 4321,
                    "hostname": "other-host",
                })
            record = _resume_record(saved)

            with chdir(current), _canonical(record), \
                    self.assertRaises(CLI.store.SessionBusyError):
                CLI.resume_session_record(
                    sess, record,
                    cwd_policy="current", replay=False)

            self.assertEqual(sess.calls, [("acquire", "target")])
            self.assertEqual(sess.ag.session_id, "current")
            self.assertEqual(sess._leased_session_id, "current")

    def test_failure_after_target_acquire_rolls_back_target_not_current(self):
        with tempfile.TemporaryDirectory() as root:
            current = Path(root) / "current"
            saved = Path(root) / "saved"
            current.mkdir()
            saved.mkdir()
            sess = FakeResumeSession(current)
            original_messages = list(sess.ag.messages)
            sess.save_error = OSError("save failed")
            record = _resume_record(saved)

            with chdir(current), _canonical(record), \
                    self.assertRaisesRegex(OSError, "save failed"):
                CLI.resume_session_record(
                    sess, record,
                    cwd_policy="current", replay=False)

            self.assertEqual(sess.ag.session_id, "current")
            self.assertEqual(sess.ag.messages, original_messages)
            self.assertEqual(sess._leased_session_id, "current")
            self.assertIn(("rollback", "target"), sess.calls)
            self.assertNotIn(("commit", "target"), sess.calls)

    def test_acquire_is_followed_by_fresh_canonical_read(self):
        with tempfile.TemporaryDirectory() as root:
            current = Path(root) / "current"
            saved = Path(root) / "saved"
            current.mkdir()
            saved.mkdir()
            sess = FakeResumeSession(current)
            stale = _resume_record(saved)
            stale["messages"][1]["content"] = "STALE"
            fresh = _resume_record(saved)
            fresh["messages"][1]["content"] = "FRESH"

            with chdir(current), _canonical(
                    fresh,
                    on_load=lambda session_id: sess.calls.append(
                        ("load", session_id))), \
                    mock.patch.object(CLI, "_session_notice"):
                CLI.resume_session_record(
                    sess, stale, cwd_policy="saved", replay=False)

            self.assertLess(
                sess.calls.index(("acquire", "target")),
                sess.calls.index(("load", "target")),
            )
            self.assertEqual(sess.ag.messages[1]["content"], "FRESH")

    def test_commit_failure_restores_old_runtime_and_rolls_back_target(self):
        with tempfile.TemporaryDirectory() as root:
            current = Path(root) / "current"
            saved = Path(root) / "saved"
            current.mkdir()
            saved.mkdir()
            sess = FakeResumeSession(current)
            sess.commit_error = OSError("old lease release failed")
            record = _resume_record(saved)

            with chdir(current), _canonical(record), \
                    self.assertRaisesRegex(OSError, "release failed"):
                CLI.resume_session_record(
                    sess, record, cwd_policy="current", replay=False)

            self.assertEqual(sess.ag.session_id, "current")
            self.assertEqual(sess._leased_session_id, "current")
            self.assertIn(("rollback", "target"), sess.calls)
            self.assertNotIn(("detach",), sess.calls)
            self.assertNotIn(("reset-agents",), sess.calls)

    def test_legacy_record_does_not_inherit_plan_mode(self):
        with tempfile.TemporaryDirectory() as root:
            sess = FakeResumeSession(root)
            sess.plan_mode = True
            record = _resume_record(root)
            record.pop("plan_mode", None)

            CLI.restore_session_record(sess, record)

        self.assertFalse(sess.plan_mode)

    def test_resume_restores_only_target_session_grants(self):
        current = Path.cwd()
        sess = FakeResumeSession(current)
        sess.always = {"edit_file"}
        record = _resume_record(current)
        record["session_grants"] = ["bash"]

        with _canonical(record):
            self.assertTrue(CLI.resume_session_record(
                sess, {"id": record["id"]}, replay=False))

        self.assertEqual(sess.always, {"bash"})

    def test_cwd_fork_holds_source_lease_until_branch_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            current = root / "current"
            saved = root / "saved"
            current.mkdir()
            saved.mkdir()
            sess = FakeResumeSession(current)
            record = _resume_record(saved)
            branch = {"id": "forked001"}

            def create_branch(*_args, **_kwargs):
                self.assertNotIn(("rollback", record["id"]), sess.calls)
                return branch

            with mock.patch.object(CLI.os, "getcwd", return_value=str(current)), \
                 _canonical(record), \
                 mock.patch.object(
                     CLI, "fork_session_record",
                     side_effect=create_branch) as fork:
                switched = CLI.resume_session_record(
                    sess, {"id": record["id"]}, cwd_policy="fork",
                    replay=False)

        self.assertTrue(switched)
        self.assertIn(("rollback", record["id"]), sess.calls)
        fork.assert_called_once_with(
            sess, record, cwd=str(current), replay=False,
            save_current=True)


class NewSessionTransactionTests(unittest.TestCase):
    def test_commit_failure_restores_old_runtime_before_cleanup(self):
        with tempfile.TemporaryDirectory() as root:
            sess = FakeResumeSession(root)
            old_agent = sess.ag
            old_controller = sess.controller
            new_agent = FakeAgent("new")
            sess.commit_error = OSError("old lease release failed")

            with mock.patch.object(CLI.A, "Agent", return_value=new_agent), \
                    self.assertRaisesRegex(OSError, "release failed"):
                CLI.cmd_new(sess, "")

        self.assertIs(sess.ag, old_agent)
        self.assertIs(sess.controller, old_controller)
        self.assertEqual(sess.last_ctx, old_agent.last_total)
        self.assertEqual(sess._title, "current title")
        self.assertEqual(sess._leased_session_id, "current")
        self.assertEqual(sess.calls, [
            ("acquire", "new"),
            ("save", "current"),
            ("reset-controller", "new"),
            ("commit", "new"),
            ("rollback", "new"),
        ])

    def test_cleanup_runs_only_after_successful_commit(self):
        with tempfile.TemporaryDirectory() as root:
            sess = FakeResumeSession(root)
            new_agent = FakeAgent("new")

            with mock.patch.object(CLI.A, "Agent", return_value=new_agent), \
                    mock.patch("builtins.print"):
                CLI.cmd_new(sess, "")

        self.assertIs(sess.ag, new_agent)
        self.assertEqual(sess.controller, ("controller", "new"))
        self.assertEqual(sess._leased_session_id, "new")
        self.assertEqual(sess.calls, [
            ("acquire", "new"),
            ("save", "current"),
            ("reset-controller", "new"),
            ("commit", "new"),
            ("detach",),
            ("reset-agents",),
        ])
        self.assertIs(new_agent.hook_cfg, sess.cfg)
        self.assertIs(new_agent.confirm, sess.confirm)
        self.assertIs(
            new_agent.permission_decision, sess.permission_decision)


class SessionLeaseTransferTests(unittest.TestCase):
    def test_old_release_failure_keeps_old_pointer_and_target_rollbackable(self):
        sess = CLI.Session.__new__(CLI.Session)
        sess.lease_owner_id = "owner"
        sess._leased_session_id = "current"
        sess._pending_session_leases = {"target"}
        sess._last_lease_heartbeat = 0.0

        with mock.patch.object(
                CLI.store, "heartbeat_session_lease", return_value={}), \
                mock.patch.object(
                    CLI.store, "release_session_lease",
                    side_effect=[OSError("unlink failed"), True]) as release:
            with self.assertRaisesRegex(OSError, "unlink failed"):
                sess.commit_target_lease("target")

            self.assertEqual(sess._leased_session_id, "current")
            self.assertIn("target", sess._pending_session_leases)
            self.assertTrue(sess.rollback_target_lease("target"))

        self.assertNotIn("target", sess._pending_session_leases)
        self.assertEqual(
            [call.args[0] for call in release.call_args_list],
            ["current", "target"],
        )


class SessionWorkspaceResetTests(unittest.TestCase):
    def test_journal_failure_still_installs_fresh_workspace_and_continues(self):
        events = [
            {
                "kind": "agent_failed",
                "payload": {
                    "id": "child-1",
                    "parent_session_id": "current",
                },
            },
            {
                "kind": "agent_completed",
                "payload": {
                    "id": "child-2",
                    "parent_session_id": "current",
                },
            },
        ]

        class OldWorkspace:
            closed = False

            def close(self):
                self.closed = True
                return True

            def drain(self):
                return list(events)

        old_workspace = OldWorkspace()
        fresh_workspace = object()
        controller = mock.Mock()
        controller.record_event.side_effect = [
            OSError("journal failed"), None]
        sess = CLI.Session.__new__(CLI.Session)
        sess.ag = FakeAgent()
        sess.controller = controller
        sess.attached_agent_id = None
        sess.agent_workspace = old_workspace
        sess._agent_cache = {"child": {}}
        sess._agent_cache_at = {"child": 1.0}
        sess._agent_projection_changed = True
        sess._main_draft = "main draft"
        sess._agent_drafts = {"child": "agent draft"}

        with mock.patch.object(
                CLI.AGENTS, "AgentWorkspace",
                return_value=fresh_workspace), \
                mock.patch.object(CLI.store, "log") as log:
            sess.reset_agent_workspace(
                previous_session_id="current",
                previous_controller=controller,
            )

        self.assertTrue(old_workspace.closed)
        self.assertIs(sess.agent_workspace, fresh_workspace)
        self.assertIsNone(sess.attached_agent_id)
        self.assertEqual(sess._agent_cache, {})
        self.assertEqual(sess._agent_cache_at, {})
        self.assertFalse(sess._agent_projection_changed)
        self.assertEqual(sess._main_draft, "")
        self.assertEqual(sess._agent_drafts, {})
        self.assertEqual(
            [call.args[0] for call in controller.record_event.call_args_list],
            ["agent_failed", "agent_completed"],
        )
        log.assert_called_once()
        self.assertEqual(
            (log.call_args.args[0], log.call_args.kwargs["phase"],
             log.call_args.kwargs["event_kind"]),
            ("agent_workspace_reset_failed", "journal", "agent_failed"),
        )


class SessionLeaseRepairCommandTests(unittest.TestCase):
    def _session(self, confirmation):
        sess = mock.Mock()
        sess.prompt_text.return_value = confirmation
        return sess

    def test_repair_lease_requires_exact_id_then_quarantines(self):
        session_id = "20260825-120000-deadbeef"
        sess = self._session(session_id)
        destination = Path("/tmp/quarantine") / f"{session_id}.json"

        with mock.patch.object(
                CLI.store, "session_lease",
                return_value={"_corrupt": True}), \
                mock.patch.object(
                    CLI.store, "quarantine_corrupt_session_lease",
                    return_value=destination) as quarantine, \
                mock.patch.object(CLI, "_session_notice") as notice:
            CLI.cmd_sessions(sess, f"repair-lease {session_id}")

        sess.prompt_text.assert_called_once()
        quarantine.assert_called_once_with(session_id)
        self.assertIn(str(destination), notice.call_args.args[1])

    def test_repair_lease_cancels_when_confirmation_does_not_match(self):
        session_id = "20260825-120000-deadbeef"
        sess = self._session("wrong-id")

        with mock.patch.object(
                CLI.store, "session_lease",
                return_value={"_corrupt": True}), \
                mock.patch.object(
                    CLI.store, "quarantine_corrupt_session_lease") \
                    as quarantine, \
                mock.patch.object(CLI, "_session_notice") as notice:
            CLI.cmd_sessions(sess, f"repair-lease {session_id}")

        quarantine.assert_not_called()
        self.assertIn("已取消", notice.call_args.args[1])


if __name__ == "__main__":
    unittest.main(verbosity=2)
