"""后台任务（Ctrl+B）结束后，结果自动排成一条 prompt 交回模型继续（2026-09-04 用户要求）。

以前只有界面通知与日志事件，模型不会自己接着干；workflow 早有同款交接，这里对齐。
"""
import io
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import zylab
from core import controller as C, settings


def make_sess(cfg=None, handoff_id=None):
    sess = object.__new__(zylab.Session)
    sess.cfg = cfg if cfg is not None else {}
    sess.controller = mock.Mock()
    sess.controller.submit.return_value = "ACTION"
    sess.task_manager = mock.Mock()
    sess._task_background_handoff_id = handoff_id
    sess._background_task_context = {}
    sess.attached_task_id = None
    sess._task_handoff_queue = []
    sess._task_handoff_ids = set()
    return sess


def finished(task_id, status="completed", background=True, result="tests passed", returncode=0):
    return {
        "t": status, "task_id": task_id, "v": result,
        "task": {
            "id": task_id, "key": "call-1", "name": "bash", "background": background,
            "status": status, "returncode": returncode, "signal": None, "timed_out": False,
            "stdout_bytes": len(result), "stderr_bytes": 0,
            "stdout_path": "/tmp/t/stdout.log", "stderr_path": None,
        },
    }


def drain(sess, *events):
    sess.task_manager.drain_background.return_value = list(events)
    return sess.drain_task_events()


class HandoffTests(unittest.TestCase):
    def test_finished_background_task_is_handed_back_to_the_model_once(self):
        sess = make_sess()
        notices = drain(sess, finished("t1"))
        self.assertTrue(any("结果已排入主 agent" in n["text"] for n in notices))
        self.assertEqual(len(sess._task_handoff_queue), 1)
        action = sess.dispatch_task_handoff_ready()
        self.assertEqual(action, "ACTION")
        prompt, mode = sess.controller.submit.call_args.args
        self.assertEqual(mode, C.QueueMode.NEXT_TURN)
        self.assertIn("[后台任务 t1 已完成", prompt)
        self.assertIn("tests passed", prompt)
        self.assertIn("/tmp/t/stdout.log", prompt)
        self.assertIn("不要重复它已做的事", prompt)
        self.assertTrue(prompt.startswith("[系统通知 · 非用户输入"), "首行标注非用户输入（J6）")
        self.assertIsNone(sess.dispatch_task_handoff_ready(), "队列排空后返回 None")
        drain(sess, finished("t1"))
        self.assertEqual(sess._task_handoff_queue, [], "同一任务不交接第二次")

    def test_failed_task_carries_exit_code(self):
        sess = make_sess()
        drain(sess, finished("t2", status="failed", returncode=2, result="boom"))
        prompt = sess._task_handoff_queue[0]["prompt"]
        self.assertIn("已失败", prompt)
        self.assertIn("exit 2", prompt)
        self.assertIn("boom", prompt)

    def test_cancelled_foreground_and_disabled_do_not_hand_off(self):
        sess = make_sess()
        drain(sess, finished("t3", status="cancelled"))
        drain(sess, finished("t4", background=False))
        self.assertEqual(sess._task_handoff_queue, [])
        off = make_sess(cfg={"background_task_handoff": False})
        notices = drain(off, finished("t5"))
        self.assertEqual(off._task_handoff_queue, [])
        self.assertTrue(any("task t5" in n["text"] for n in notices), "通知照样打")

    def test_long_output_keeps_the_tail_and_points_to_the_file(self):
        sess = make_sess()
        drain(sess, finished("t6", result="x" * 9000))
        prompt = sess._task_handoff_queue[0]["prompt"]
        self.assertIn("已省略", prompt)
        self.assertLess(len(prompt), 7000)
        self.assertIn("完整记录：/tmp/t/stdout.log", prompt)

    def test_setting_is_on_by_default_and_visible_in_config(self):
        self.assertTrue(settings.DEFAULTS["background_task_handoff"])
        self.assertIn("background_task_handoff", settings.render(dict(settings.DEFAULTS), ["builtin"]))


if __name__ == "__main__":
    unittest.main()
