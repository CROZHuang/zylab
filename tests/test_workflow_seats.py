"""用户可配席位（用户 2026-09-04："比如我想添加 minimax 和 deepseek flash，去掉 qwen"）。

settings workflow.seats 只写变化的部分；/workflow seats 二级指令增删改；schema 枚举与
配方校验跟着生效席位表走。每个用例结束都把席位表复原，避免污染其他测试。
"""
import io
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import zylab
from core import models, recipes, settings, tools

BUILTIN = ("Kimi", "GLM", "DeepSeek", "Qwen")
OVERRIDES = {
    "MiniMax": ["deepinfer/minimax-m2.7"],
    "DeepSeekFlash": {"family": "deepseek", "candidates": [["deepinfer", "deepseek-v4-flash"]]},
    "Qwen": None,
}


class _Restore(unittest.TestCase):
    def tearDown(self):
        models.apply_workflow_seat_overrides({})
        tools.sync_workflow_seats()
        try:
            settings.update_user_workflow_seats(None)
        except settings.SettingsError:
            pass


class SettingsTests(_Restore):
    def test_user_config_can_add_replace_and_remove(self):
        policy = settings.workflow_policy({"workflow": {"seats": OVERRIDES}})
        seats = policy["seats"]
        self.assertEqual(seats["MiniMax"]["candidates"], ["deepinfer/minimax-m2.7"])
        self.assertEqual(seats["DeepSeekFlash"]["candidates"], ["deepinfer/deepseek-v4-flash"])
        self.assertEqual(seats["DeepSeekFlash"]["family"], "deepseek")
        self.assertIsNone(seats["Qwen"])

    def test_malformed_entries_are_ignored_not_fatal(self):
        out = io.StringIO()
        with mock.patch.object(sys, "stdout", out):
            policy = settings.workflow_policy({"workflow": {"seats": {
                "bad name!": ["deepinfer/x"], "NoSlash": ["deepinfer-x"], "Empty": []}}})
        self.assertEqual(policy["seats"], {})
        self.assertIn("已忽略", out.getvalue())

    def test_project_config_may_only_remove(self):
        cfg = {"workflow": {"seats": {}}}
        out = io.StringIO()
        with mock.patch.object(sys, "stdout", out):
            settings._merge_workflow(cfg["workflow"], {"seats": OVERRIDES}, trusted_user=False)
        self.assertEqual(cfg["workflow"]["seats"], {"Qwen": None})
        self.assertIn("项目级配置不能新增", out.getvalue())

    def test_user_file_update_touches_only_seats(self):
        settings.write_user({"workflow": {"preflight": False}})
        settings.update_user_workflow_seats({"MiniMax": ["deepinfer/minimax-m2.7"]})
        cfg, _ = settings.load()
        self.assertFalse(cfg["workflow"]["preflight"], "同段其他键不能被覆盖掉")
        self.assertEqual(cfg["workflow"]["seats"]["MiniMax"]["candidates"], ["deepinfer/minimax-m2.7"])
        settings.update_user_workflow_seats(None)
        cfg, _ = settings.load()
        self.assertEqual(cfg["workflow"]["seats"], {})
        self.assertFalse(cfg["workflow"]["preflight"])


class ModelTableTests(_Restore):
    def test_overrides_rebuild_the_effective_table_in_order(self):
        seats, problems = models.apply_workflow_seat_overrides(
            settings.workflow_policy({"workflow": {"seats": OVERRIDES}})["seats"])
        self.assertEqual(problems, [])
        self.assertEqual(models.WORKFLOW_SEAT_NAMES, ("Kimi", "GLM", "DeepSeek", "MiniMax", "DeepSeekFlash"))
        by_seat = {item["seat"]: item for item in seats}
        self.assertEqual(by_seat["MiniMax"]["candidates"], (("deepinfer", "minimax-m2.7"),))
        self.assertEqual(by_seat["DeepSeekFlash"]["family"], "deepseek")
        self.assertEqual(models.workflow_seat_source("MiniMax"), "user")
        self.assertEqual(models.workflow_seat_source("Kimi"), "builtin")

    def test_replacing_a_builtin_seat_keeps_its_position(self):
        models.apply_workflow_seat_overrides({"GLM": ["boyue/glm-5.3"]})
        self.assertEqual(models.WORKFLOW_SEAT_NAMES, BUILTIN)
        glm = next(item for item in models.WORKFLOW_DEFAULT_SEATS if item["seat"] == "GLM")
        self.assertEqual(glm["candidates"], (("boyue", "glm-5.3"),))
        self.assertEqual(models.workflow_seat_source("GLM"), "user（替换内置）")

    def test_unknown_gateway_is_reported_and_skipped(self):
        seats, problems = models.apply_workflow_seat_overrides({"X": ["nowhere/model"]})
        self.assertEqual(models.WORKFLOW_SEAT_NAMES, BUILTIN)
        self.assertTrue(any("nowhere/model" in p for p in problems), problems)

    def test_reset_restores_builtin(self):
        models.apply_workflow_seat_overrides(OVERRIDES)
        models.apply_workflow_seat_overrides({})
        self.assertEqual(models.WORKFLOW_SEAT_NAMES, BUILTIN)

    def test_schema_enum_and_recipe_validation_follow_the_table(self):
        models.apply_workflow_seat_overrides(OVERRIDES)
        names = tools.sync_workflow_seats()
        schema = next(row for row in tools.SCHEMA if row["function"]["name"] == "workflow")
        enum = schema["function"]["parameters"]["properties"]["agents"]["items"]["properties"]["seat"]["enum"]
        self.assertEqual(enum, names)
        self.assertIn("MiniMax", enum)
        self.assertNotIn("Qwen", enum)
        recipe = {
            "name": "seat-test", "description": "d", "version": 1,
            "workflow": {"goal": "g", "agents": [
                {"key": "a", "seat": "MiniMax", "task": "t"},
                {"key": "b", "seat": "Kimi", "task": "t"}]}}
        recipes.validate(recipe, source="<test>")
        recipe["workflow"]["agents"][0]["seat"] = "Qwen"
        with self.assertRaises(recipes.RecipeError):
            recipes.validate(recipe, source="<test>")


class CommandTests(_Restore):
    def make_session(self):
        sess = object.__new__(zylab.Session)
        sess.cfg = {"workflow": {"seats": {}}}
        sess.workflow_manager = mock.Mock()
        sess.workflow_manager.resolve_lineup.return_value = []
        sess.pump = None
        return sess

    def run_cmd(self, sess, text):
        out = io.StringIO()
        with mock.patch.object(sys, "stdout", out):
            zylab._workflow_seats_command(sess, text)
        return out.getvalue()

    def test_add_remove_list_reset_persist_and_apply_immediately(self):
        sess = self.make_session()
        text = self.run_cmd(sess, "add MiniMax deepinfer/minimax-m2.7")
        self.assertIn("MiniMax ← deepinfer/minimax-m2.7", text)
        self.assertIn("MiniMax", models.WORKFLOW_SEAT_NAMES)
        text = self.run_cmd(sess, "remove Qwen")
        self.assertIn("已移除 Qwen", text)
        self.assertNotIn("Qwen", models.WORKFLOW_SEAT_NAMES)
        cfg, _ = settings.load()
        self.assertEqual(set(cfg["workflow"]["seats"]), {"MiniMax", "Qwen"})
        text = self.run_cmd(sess, "list")
        self.assertIn("MiniMax", text)
        self.assertIn("已移除（用户配置）：Qwen", text)
        text = self.run_cmd(sess, "reset")
        self.assertIn("恢复内置席位", text)
        self.assertEqual(models.WORKFLOW_SEAT_NAMES, BUILTIN)

    def test_invalid_route_is_rejected_without_writing(self):
        sess = self.make_session()
        text = self.run_cmd(sess, "add Flash nowhere/deepseek-v4-flash")
        self.assertIn("无效", text)
        self.assertNotIn("Flash", models.WORKFLOW_SEAT_NAMES)
        cfg, _ = settings.load()
        self.assertEqual(cfg["workflow"]["seats"], {})

    def test_seats_is_a_guided_subcommand_with_options(self):
        tree = zylab.COMMAND_SUBCOMMANDS["/workflow"]
        items = dict(tree["items"])
        self.assertIn("seats", items)
        for retired in ("quick", "standard", "deep", "review", "research", "debug"):
            self.assertNotIn(retired, items)
        options = dict(tree["params"]["seats"]["items"])
        self.assertEqual(set(options), {"list", "add", "remove", "reset"})

    def test_suggest_seat_name(self):
        self.assertEqual(zylab._suggest_seat_name({"family": "MiniMax", "id": "minimax-m2.7"}), "MiniMax")
        self.assertEqual(zylab._suggest_seat_name({"family": "other", "id": "deepseek-v4-flash"}), "Other")
        self.assertTrue(zylab._suggest_seat_name({"id": "9x"})[0].isalpha())


if __name__ == "__main__":
    unittest.main()
