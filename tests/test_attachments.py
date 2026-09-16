import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from unittest import mock as _mock
from core import attachments
from core import context
from core import tui


class AttachmentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.snapshots = self.root / "snapshots"
        self.root_patch = mock.patch.object(
            attachments, "ROOT", self.snapshots)
        self.root_patch.start()

    def tearDown(self):
        self.root_patch.stop()
        self.temp.cleanup()

    def prepare(self, text, **kwargs):
        return attachments.prepare_user_message(
            text, cwd=self.root, root=self.snapshots, **kwargs)

    def test_reference_parser_does_not_treat_email_as_file(self):
        self.assertEqual(attachments.references("mail a@example.com"), [])
        refs = attachments.references('read @a.txt and @"b c.txt"')
        self.assertEqual([row["path"] for row in refs], ["a.txt", "b c.txt"])

    def test_text_uses_private_immutable_snapshot(self):
        source = self.root / "note.txt"
        source.write_text("alpha evidence", encoding="utf-8")
        message = self.prepare("review @note.txt", supports_image=False)
        source.write_text("changed later", encoding="utf-8")

        projected = attachments.materialize_provider_messages([message])
        self.assertIn("alpha evidence", projected[0]["content"])
        self.assertNotIn("changed later", projected[0]["content"])
        self.assertEqual(message["content"], "review @note.txt")
        self.assertNotIn("alpha evidence", json.dumps(message))
        snapshot = Path(message["_kc_attachments"][0]["snapshot_path"])
        self.assertEqual(os.stat(snapshot).st_mode & 0o777, 0o600)

    def test_tampered_snapshot_fails_closed(self):
        source = self.root / "note.txt"
        source.write_text("trusted", encoding="utf-8")
        message = self.prepare("@note.txt", supports_image=False)
        snapshot = Path(message["_kc_attachments"][0]["snapshot_path"])
        snapshot.write_text("tampered", encoding="utf-8")
        with self.assertRaisesRegex(
                attachments.AttachmentError, "哈希不匹配"):
            attachments.materialize_provider_messages([message])

    def test_image_requires_capability_and_materializes_data_url(self):
        image = self.root / "tiny.png"
        image.write_bytes(b"\x89PNG\r\n\x1a\n" + b"test")
        with self.assertRaisesRegex(
                attachments.AttachmentError, "图片输入能力"):
            self.prepare("look @tiny.png", supports_image=None)
        message = self.prepare("look @tiny.png", supports_image=True)
        projected = attachments.materialize_provider_messages([message])
        self.assertEqual(projected[0]["content"][0]["type"], "text")
        url = projected[0]["content"][1]["image_url"]["url"]
        self.assertTrue(url.startswith("data:image/png;base64,"))
        self.assertEqual(attachments.stats([message])["image_token_estimate"],
                         attachments.IMAGE_TOKEN_ESTIMATE)

    def test_large_text_is_explicitly_truncated(self):
        source = self.root / "large.txt"
        source.write_text("a" * 60, encoding="utf-8")
        with mock.patch.object(attachments, "MAX_TEXT_CHARS", 20), \
                mock.patch.object(attachments, "MAX_TOTAL_TEXT_CHARS", 100):
            message = self.prepare("@large.txt", supports_image=False)
            projected = attachments.materialize_provider_messages([message])
        item = message["_kc_attachments"][0]
        self.assertTrue(item["truncated"])
        self.assertEqual(item["omitted_chars"], 40)
        self.assertIn("显式截断", projected[0]["content"])

    def test_read_size_is_rechecked_after_stat(self):
        source = self.root / "growing.txt"
        source.write_text("x", encoding="utf-8")
        with mock.patch.object(attachments, "MAX_TEXT_SOURCE_BYTES", 4), \
                mock.patch.object(attachments, "MAX_IMAGE_BYTES", 4), \
                mock.patch.object(Path, "read_bytes", return_value=b"12345"):
            with self.assertRaisesRegex(
                    attachments.AttachmentError, "读取后超过上限"):
                self.prepare("@growing.txt", supports_image=False)

    def test_protected_and_likely_credentials_are_rejected(self):
        with _mock.patch.dict(
                os.environ,
                {"ZYLAB_PROTECTED_PATHS": "/protected/archive"}), \
                self.assertRaisesRegex(
                attachments.AttachmentError, "只读归档"):
            self.prepare("@/protected/archive/not-present.txt",
                         supports_image=False)
        secret = self.root / ".env"
        secret.write_text("API_KEY=secret", encoding="utf-8")
        with self.assertRaisesRegex(
                attachments.AttachmentError, "潜在凭据"):
            self.prepare("@.env", supports_image=False)

    def test_context_report_counts_attachment_and_keeps_raw_user_text(self):
        source = self.root / "paper.txt"
        source.write_text("research-evidence", encoding="utf-8")
        user = self.prepare("study @paper.txt", supports_image=False)
        projection = context.materialize(
            [{"role": "system", "content": "s"}, user],
            tools_schema=[], model_limit=20_000, usable_budget=18_000)
        self.assertIn("research-evidence", projection.messages[-1]["content"])
        self.assertEqual(projection.report["attachments"]["text_count"], 1)
        self.assertGreater(projection.report["components"]["attachments"], 0)
        raw_users = [message["content"] for message in [user]]
        projected_users = [
            message["content"] for message in projection.messages
            if message["role"] == "user"]
        self.assertTrue(projected_users[0].startswith(raw_users[0]))

    def test_valid_summary_stops_reinjecting_covered_attachment_body(self):
        source = self.root / "paper.txt"
        source.write_text("old-full-evidence", encoding="utf-8")
        old_user = self.prepare("read @paper.txt", supports_image=False)
        messages = [
            {"role": "system", "content": "s"},
            old_user,
            {"role": "assistant", "content": "used evidence"},
            {"role": "user", "content": "continue"},
        ]
        plan = context.plan_compaction_to(messages, 3)
        summary = context.make_summary(
            "evidence summarized", plan, model="m", gateway="g")
        projection = context.materialize(
            messages, summary=summary, tools_schema=[],
            model_limit=20_000, usable_budget=18_000)
        payload = json.dumps(projection.messages, ensure_ascii=False)
        self.assertNotIn("old-full-evidence", payload)
        users = [message for message in projection.messages
                 if message["role"] == "user"]
        self.assertEqual([message["content"] for message in users],
                         ["read @paper.txt", "continue"])

    def test_line_editor_uses_existing_menu_for_bounded_path_completion(self):
        (self.root / "alpha.txt").write_text("a", encoding="utf-8")
        (self.root / "beta.txt").write_text("b", encoding="utf-8")
        editor = tui.LineEditor(cwd=self.root)
        editor.replace("review @al")
        snapshot = editor.snapshot()
        self.assertEqual(snapshot.options,
                         (("review @alpha.txt", "file"),))
        event = editor.handle("tab")[0]
        self.assertEqual(event.kind, "redraw")
        self.assertEqual(editor.text, "review @alpha.txt ")

    def test_busy_tab_completes_at_file_before_queue_submission(self):
        (self.root / "paper.md").write_text("p", encoding="utf-8")
        editor = tui.LineEditor(cwd=self.root)
        editor.set_busy(True)
        editor.replace("use @pap")
        events = editor.handle("tab")
        self.assertEqual([event.kind for event in events], ["redraw"])
        self.assertEqual(editor.text, "use @paper.md ")

    def test_completion_navigates_directory_with_spaces(self):
        nested = self.root / "paper set"
        nested.mkdir()
        (nested / "notes.md").write_text("n", encoding="utf-8")
        editor = tui.LineEditor(cwd=self.root)
        editor.replace("review @paper")

        editor.handle("tab")
        self.assertEqual(editor.text, 'review @"paper set/')
        self.assertEqual(
            editor.snapshot().options,
            (('review @"paper set/notes.md"', "file"),))
        editor.handle("tab")
        self.assertEqual(editor.text, 'review @"paper set/notes.md" ')


if __name__ == "__main__":
    unittest.main()
