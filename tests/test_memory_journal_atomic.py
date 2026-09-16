"""F5：MemoryJournal.append_events 必须整批成功或整批不动。"""
import os
import sys
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from core import controller


class AtomicAppendTests(unittest.TestCase):
    def setUp(self):
        self.j = controller.MemoryJournal()
        self.j.append_events("s", [{"kind": "seed", "payload": {"n": 0}}])

    def test_bad_event_mid_batch_changes_nothing(self):
        before = ([dict(r) for r in self.j.events], self.j._seq)
        with self.assertRaises(controller.ControllerError):
            self.j.append_events("s", [
                {"kind": "a", "payload": {}},
                {"kind": "", "payload": {}},           # 非法：kind 为空
                {"kind": "c", "payload": {}},
            ])
        self.assertEqual(([dict(r) for r in self.j.events], self.j._seq), before)

    def test_non_dict_payload_is_rejected_whole(self):
        before = len(self.j.events)
        with self.assertRaises(controller.ControllerError):
            self.j.append_events("s", [
                {"kind": "a", "payload": {}},
                {"kind": "b", "payload": ["not", "a", "dict"]},
            ])
        self.assertEqual(len(self.j.events), before)

    def test_uncopyable_payload_is_rejected_whole(self):
        before = (len(self.j.events), self.j._seq)
        with self.assertRaises(controller.ControllerError):
            self.j.append_events("s", [
                {"kind": "a", "payload": {}},
                # deepcopy 把函数当原子值直接复用，不会报错；锁才是真的复制不了。
                {"kind": "b", "payload": {"lock": threading.Lock()}},
            ])
        self.assertEqual((len(self.j.events), self.j._seq), before)

    def test_sequence_is_contiguous_after_a_rejected_batch(self):
        with self.assertRaises(controller.ControllerError):
            self.j.append_events("s", [{"kind": "x"}, {"kind": ""}])
        rows = self.j.append_events("s", [{"kind": "y"}, {"kind": "z"}])
        self.assertEqual([r["seq"] for r in rows], [2, 3])
        self.assertEqual([r["seq"] for r in self.j.events], [1, 2, 3])

    def test_nested_payload_mutation_after_append_does_not_leak(self):
        payload = {"outer": {"inner": "before"}, "items": [1]}
        self.j.append_events("s", [{"kind": "k", "payload": payload}])
        payload["outer"]["inner"] = "after"
        payload["items"].append(2)
        stored = self.j.events[-1]["payload"]
        self.assertEqual(stored, {"outer": {"inner": "before"}, "items": [1]})

    def test_returned_rows_are_detached_from_the_journal(self):
        rows = self.j.append_events("s", [{"kind": "k", "payload": {"a": {"b": 1}}}])
        rows[0]["payload"]["a"]["b"] = 999
        self.assertEqual(self.j.events[-1]["payload"], {"a": {"b": 1}})


if __name__ == "__main__":
    unittest.main()
