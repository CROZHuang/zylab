import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from unittest import mock as _mock
from core import agents, recipes, workflows
from tests.platform_support import requires_symlinks  # noqa: E402


def lineup():
    seats = ("Qwen", "GLM", "DeepSeek", "MiniMax", "Kimi")
    gateways = ("boyue", "boyue", "deepinfer", "deepinfer", "deepinfer")
    return [{
        "seat": seat, "family": seat, "id": seat.lower(),
        "gateway": gateway, "status": "ok", "supports_tools": True,
        "quality_tier": "flagship",
    } for seat, gateway in zip(seats, gateways)]


class RecipeAgent:
    def __init__(self, model, gateway=None, load_md=True):
        self.model = model
        self.gateway = gateway
        self.messages = []
        self.tokens_in = 0
        self.tokens_out = 0
        self.last_total = 0

    def load_context(self, _value):
        return None

    def context_snapshot(self):
        return None

    def run(self, user_input, *, event_sink=None, **_kwargs):
        guard = _kwargs.pop("before_provider_attempt", None)
        if guard is not None:
            guard({
                "attempt": 1, "purpose": "chat",
                "model": self.model, "gateway": self.gateway,
            })
        user = {"role": "user", "content": str(user_input)}
        assistant = {"role": "assistant", "content": "recipe evidence"}
        self.messages.extend((user, assistant))
        event_sink([])
        yield {"t": "text", "v": "recipe evidence"}
        yield {"t": "end", "reason": "stop"}


class RecipeTests(unittest.TestCase):
    def test_malformed_dependency_and_oversized_file_fail_visible(self):
        invalid = json.loads(json.dumps(recipes.BUILTINS["debug-triangulate"]))
        invalid["workflow"]["agents"][1]["depends_on"] = [123]
        with self.assertRaisesRegex(recipes.RecipeError, "depends_on"):
            recipes.validate(invalid)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "workflow-recipes"
            root.mkdir()
            (root / "huge.json").write_text("12345", encoding="utf-8")
            with mock.patch.object(recipes, "USER_ROOT", root), \
                    mock.patch.object(recipes, "MAX_RECIPE_BYTES", 4):
                rows = recipes.list_recipes(Path(tmp) / "project")
        error = next(row for row in rows
                     if row.get("source", "").endswith("huge.json"))
        self.assertIn("超过 4 bytes", error["error"])

    def test_builtin_parameter_substitution_is_literal_not_code_eval(self):
        recipe = recipes.resolve("builtin:research-review", "/tmp")
        payload = '$(touch /tmp/never) "quoted"'
        spec = recipes.instantiate(recipe, {"goal": payload})

        self.assertEqual(spec["goal"], payload)
        self.assertIn(payload, spec["agents"][0]["task"])
        self.assertEqual(spec["recipe"]["name"], "research-review")
        with self.assertRaisesRegex(recipes.RecipeError, "未知参数"):
            recipes.instantiate(recipe, {"goal": "x", "extra": "y"})
        with self.assertRaisesRegex(recipes.RecipeError, "缺少必需参数"):
            recipes.instantiate(recipe, {})

    def test_project_roundtrip_and_duplicate_requires_explicit_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            record = {
                "goal": "audit parser", "mode": "research",
                "budget": {"max_requests": 6},
                "plan": {
                    "agents": [
                        {"key": "a", "seat": "Qwen", "task": "a",
                         "role": "code", "context": "", "depends_on": []},
                        {"key": "b", "seat": "GLM", "task": "b",
                         "role": "tests", "context": "", "depends_on": ["a"]},
                    ],
                    "review": {"enabled": False, "reviewers": [], "rounds": 1},
                    "synthesis": None,
                },
            }
            recipe = recipes.from_workflow(record, "parser-audit")
            path = recipes.save(recipe, scope="project", cwd=tmp)
            loaded = recipes.resolve(str(path), tmp)
            with self.assertRaisesRegex(recipes.RecipeError, "已存在"):
                recipes.save(recipe, scope="project", cwd=tmp)
            second = recipes.save(
                recipe, scope="project", cwd=tmp, overwrite=True)

            self.assertEqual(path, second)
            self.assertEqual(loaded["name"], "parser-audit")
            self.assertEqual(
                loaded["workflow"]["agents"][1]["depends_on"], ["a"])
            self.assertEqual(json.loads(path.read_text())["version"], 1)

    def test_name_conflict_is_never_silently_shadowed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            user_root = root / "home" / "workflow-recipes"
            project = root / "project"
            payload = json.loads(json.dumps(recipes.BUILTINS["research-review"]))
            with (
                    mock.patch.object(recipes, "USER_ROOT", user_root),
                    mock.patch.object(recipes.store, "HOME", root / "home")):
                recipes.save(payload, scope="user", cwd=project)
                with self.assertRaisesRegex(recipes.RecipeError, "个来源"):
                    recipes.resolve("research-review", project)
                selected = recipes.resolve("user:research-review", project)

            self.assertEqual(selected["scope"], "user")

    @requires_symlinks
    def test_project_parent_symlink_escape_is_rejected_before_write(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as out:
            cwd = Path(tmp)
            (cwd / ".zylab").symlink_to(Path(out), target_is_directory=True)
            recipe = json.loads(json.dumps(recipes.BUILTINS["debug-triangulate"]))
            target = Path(out) / "workflows" / "debug-triangulate.json"

            with self.assertRaisesRegex(recipes.RecipeError, "安全边界"):
                recipes.save(recipe, scope="project", cwd=cwd)
            self.assertFalse(target.exists())

    def test_recipe_trigger_allows_bounded_repeated_seats_over_five_nodes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            workspace = agents.AgentWorkspace(
                root=root / "agents", agent_factory=RecipeAgent,
                poll_interval=0.005)
            manager = workflows.WorkflowManager(
                workspace, root=root / "workflows",
                lineup_resolver=lambda **_kwargs: lineup(),
                poll_interval=0.005)
            specs = [{
                "key": f"n{index}",
                "seat": ("Qwen" if index % 2 else "GLM"),
                "task": f"task {index}",
            } for index in range(1, 7)]
            started = manager.start_plan(
                "six-node recipe", specs,
                parent_session_id="parent", trigger="recipe", budget=6,
                limits={"max_nodes": 6, "max_requests": 6})
            self.assertTrue(manager.wait(started["id"], timeout=3))
            final = manager.get(started["id"])
            manager.close()
            workspace.close()

        self.assertEqual(final["state"], "completed")
        self.assertEqual(len(final["plan"]["agents"]), 6)
        self.assertEqual(final["requests_started"], 6)

    def test_protected_recipe_save_is_rejected(self):
        recipe = json.loads(json.dumps(recipes.BUILTINS["debug-triangulate"]))
        with _mock.patch.dict(
                os.environ,
                {"ZYLAB_PROTECTED_PATHS": "/protected/archive"}), \
                self.assertRaisesRegex(
                recipes.RecipeError, "受保护路径"):
            recipes.save(recipe, scope="project",
                         cwd="/protected/archive/project")


if __name__ == "__main__":
    unittest.main()
