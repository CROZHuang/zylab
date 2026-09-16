"""repo-map：哈希缓存、关系闸、Notes 保留、零 LLM 的陈旧检测、有界注入。

全部用注入的假 complete，零网络。最重的三条断言：
- 重建只重摘要**变过的**文件（哈希缓存是整个设计的支点）；
- 自动路径（check/index_text）**零次**模型调用；
- 关系动词封闭集 —— 集外动词整条丢弃。
"""
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import repomap, settings
from tests.pty_harness import run_pty_child
import zylab as CLI


def fake_complete_factory(calls, nodes_json=None):
    def complete(messages, max_tokens=None):
        system = messages[0]["content"]
        user = messages[1]["content"]
        if "架构图" in system:
            calls.append(("synthesize", ""))
            return json.dumps(nodes_json or {"nodes": []}, ensure_ascii=False)
        rel = user.split("\n", 1)[0].replace("File: ", "")
        calls.append(("summarize", rel))
        return f"SUMMARY({rel})。它做了一些事。"
    return complete


def seed_repo(root):
    (root / "alpha.py").write_text(
        "def hello():\n    return 1\n", encoding="utf-8")
    (root / "beta.py").write_text(
        "import os\n\nclass Worker:\n    pass\n", encoding="utf-8")


NODES = {"nodes": [
    {"name": "核心系统", "type": "system",
     "summary": "两个文件协作。不变量：hello 先于 Worker。",
     "sources": ["alpha.py", "beta.py"],
     "links": [
         {"to": "设计取舍", "relation": "validates", "description": "ok"},
         {"to": "设计取舍", "relation": "relates_to", "description": "糊话"},
         {"to": "不存在的节点", "relation": "uses", "description": "x"},
     ]},
    {"name": "设计取舍", "type": "concept",
     "summary": "为什么这么拆。", "sources": ["alpha.py"], "links": []},
]}


class BuildAndCache(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        seed_repo(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def build(self, calls):
        return repomap.build(
            self.root, complete=fake_complete_factory(calls, NODES),
            model="fake", gateway="test-gw")

    def test_first_build_summarizes_every_file_once(self):
        calls = []
        manifest = self.build(calls)
        summarized = [c[1] for c in calls if c[0] == "summarize"]
        self.assertEqual(sorted(summarized), ["alpha.py", "beta.py"])
        self.assertEqual(len(manifest["nodes"]), 2)
        self.assertEqual(manifest["gateway"], "test-gw")

    def test_rebuild_only_resummarizes_changed_file(self):
        """哈希缓存是支点：没变的文件绝不再花一次模型调用。"""
        self.build([])
        (self.root / "alpha.py").write_text(
            "def hello():\n    return 2\n", encoding="utf-8")
        calls = []
        self.build(calls)
        summarized = [c[1] for c in calls if c[0] == "summarize"]
        self.assertEqual(summarized, ["alpha.py"],
                         f"只该重摘要 alpha.py，实际 {summarized}")

    def test_relation_gate_drops_invalid_and_dangling(self):
        self.build([])
        manifest = repomap.load_manifest(self.root)
        links = manifest["nodes"][0]["links"]
        self.assertEqual([l["relation"] for l in links], ["validates"],
                         "relates_to 与悬空目标都该被丢弃")

    def test_notes_survive_rebuild_verbatim(self):
        self.build([])
        manifest = repomap.load_architecture_manifest(self.root)
        node = (
            repomap.architecture_node_dir(self.root, manifest)
            / f"{repomap.slugify('核心系统')}.md")
        text = node.read_text(encoding="utf-8")
        node.write_text(
            text + "这是我手写的笔记，一个字都不能少。\n", encoding="utf-8")
        (self.root / "beta.py").write_text("x = 3\n", encoding="utf-8")
        self.build([])
        manifest = repomap.load_architecture_manifest(self.root)
        node = (
            repomap.architecture_node_dir(self.root, manifest)
            / f"{repomap.slugify('核心系统')}.md")
        self.assertIn("这是我手写的笔记，一个字都不能少。",
                      node.read_text(encoding="utf-8"))

    def test_garbage_synthesis_raises_not_writes(self):
        def garbage(messages, max_tokens=None):
            return "抱歉我不会 JSON"
        with self.assertRaises(repomap.RepoMapError):
            repomap.build(self.root, complete=garbage)


class ArchitectureGenerations(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        seed_repo(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def build(self, **kwargs):
        return repomap.build_architecture(
            self.root,
            complete=fake_complete_factory([], NODES),
            model="fake", gateway="test-gw", **kwargs)

    def test_injected_failure_keeps_previous_generation(self):
        first = self.build()
        (self.root / "alpha.py").write_text(
            "def hello():\n    return 2\n", encoding="utf-8")

        def crash(_manifest, _temporary):
            raise RuntimeError("injected-before-pointer")

        with self.assertRaisesRegex(RuntimeError, "injected-before-pointer"):
            self.build(before_commit=crash)
        current = repomap.load_architecture_manifest(self.root)
        self.assertEqual(current["generation"], first["generation"])
        self.assertTrue(
            repomap.architecture_node_dir(self.root, current).is_dir())

    def test_oversized_notes_fail_before_next_provider_call(self):
        first = self.build()
        node = (
            repomap.architecture_node_dir(self.root, first)
            / f"{repomap.slugify('核心系统')}.md")
        node.write_bytes(b"x" * (repomap.MAX_NODE_FILE_BYTES + 1))
        calls = []

        def complete(*_args, **_kwargs):
            calls.append("provider")
            return "should-not-run"

        with self.assertRaisesRegex(
                repomap.RepoMapError, "Notes.*硬上限"):
            repomap.build_architecture(
                self.root, complete=complete,
                model="fake", gateway="test-gw")
        self.assertEqual(calls, [])
        self.assertEqual(
            repomap.load_architecture_manifest(self.root)["generation"],
            first["generation"])

    def test_concurrent_notes_edit_keeps_old_generation_and_new_note(self):
        first = self.build()
        node = (
            repomap.architecture_node_dir(self.root, first)
            / f"{repomap.slugify('核心系统')}.md")
        sentinel = "\nCONCURRENT-NOTES-SENTINEL\n"

        def edit_note(_manifest, _temporary):
            with node.open("a", encoding="utf-8") as handle:
                handle.write(sentinel)

        with self.assertRaisesRegex(
                repomap.RepoMapError, "Notes 在构建期间发生变化"):
            self.build(before_commit=edit_note)
        current = repomap.load_architecture_manifest(self.root)
        self.assertEqual(current["generation"], first["generation"])
        self.assertIn(
            sentinel.strip(), node.read_text(encoding="utf-8"))

    def test_live_writer_lock_rejects_second_build(self):
        with repomap.RepoBuildLock(self.root):
            with self.assertRaisesRegex(
                    repomap.RepoMapError, "PID .*持有"):
                self.build()
        self.assertFalse(
            (repomap.architecture_root(self.root)
             / repomap.ARCH_LOCK).exists())

    def test_fresh_empty_lock_is_busy_but_old_empty_lock_recovers(self):
        lock_path = (
            repomap.architecture_root(self.root)
            / repomap.ARCH_LOCK)
        lock_path.parent.mkdir(parents=True)
        lock_path.write_text("", encoding="utf-8")
        with self.assertRaisesRegex(
                repomap.RepoMapError, "正在初始化"):
            with repomap.RepoBuildLock(self.root):
                self.fail("fresh incomplete lock must not be stolen")
        self.assertTrue(lock_path.exists())
        stale = (
            time.time() - repomap.ARCH_LOCK_INIT_GRACE - 1.0)
        os.utime(lock_path, (stale, stale))
        with repomap.RepoBuildLock(self.root):
            self.assertTrue(lock_path.exists())
        self.assertFalse(lock_path.exists())

    def test_cancel_preserves_completed_summary_cache_without_commit(self):
        cancel = threading.Event()
        calls = []

        def progress(event):
            if (event.get("stage") == "summary"
                    and event.get("state") == "result"
                    and event.get("completed") == 1):
                cancel.set()

        with self.assertRaises(repomap.RepoMapCancelled):
            repomap.build_architecture(
                self.root,
                complete=fake_complete_factory(calls, NODES),
                model="fake", gateway="test-gw",
                on_progress=progress, cancel=cancel)
        self.assertIsNone(repomap.load_architecture_manifest(self.root))
        plan = repomap.architecture_plan(
            self.root, model="fake", gateway="test-gw")
        self.assertEqual(plan["cache_hits"], 1)
        self.assertEqual(plan["cache_misses"], 1)
        self.assertEqual(
            [kind for kind, _ in calls], ["summarize"])

    def test_cancel_set_by_completed_summary_still_persists_paid_cache(self):
        cancel = threading.Event()

        def complete(messages, max_tokens=None):
            if "架构图" in messages[0]["content"]:
                return json.dumps(NODES, ensure_ascii=False)
            cancel.set()
            return "完整摘要。已经收到且应当在取消前持久化。"

        with self.assertRaises(repomap.RepoMapCancelled):
            repomap.build_architecture(
                self.root, complete=complete,
                model="fake", gateway="test-gw", cancel=cancel)
        plan = repomap.architecture_plan(
            self.root, model="fake", gateway="test-gw")
        self.assertEqual(plan["cache_hits"], 1)
        self.assertEqual(plan["cache_misses"], 1)
        self.assertIsNone(repomap.load_architecture_manifest(self.root))

    def test_cancel_set_by_completed_synthesis_keeps_raw_evidence(self):
        cancel = threading.Event()
        sentinel = "SYNTHESIS-CANCEL-SENTINEL"
        payload = json.loads(json.dumps(NODES, ensure_ascii=False))
        payload["nodes"][0]["summary"] = sentinel

        def complete(messages, max_tokens=None):
            if "架构图" in messages[0]["content"]:
                cancel.set()
                return json.dumps(payload, ensure_ascii=False)
            return "完整摘要。可以缓存。"

        with self.assertRaises(repomap.RepoMapCancelled):
            repomap.build_architecture(
                self.root, complete=complete,
                model="fake", gateway="test-gw", cancel=cancel)
        raw = (
            repomap._architecture_cache_dir(self.root)
            / "last-synthesis.txt").read_text(encoding="utf-8")
        self.assertIn(sentinel, raw)
        self.assertIsNone(repomap.load_architecture_manifest(self.root))

    def test_preflight_request_estimate_is_character_aware_upper_bound(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for index in range(40):
                (root / f"module_{index:02d}.py").write_text(
                    "value = 1\n", encoding="utf-8")
            plan = repomap.architecture_plan(
                root, model="fake", gateway="test-gw")
        self.assertEqual(plan["cache_misses"], 40)
        self.assertGreater(plan["synthesis_batches"], 1)
        self.assertEqual(plan["estimate_kind"], "upper_bound")
        self.assertEqual(
            plan["estimated_requests"],
            40 + plan["synthesis_batches"]
            + plan["consolidation_requests"])

    def test_falsey_wrong_manifest_collection_type_fails_closed(self):
        manifest = self.build()
        path = (
            repomap._architecture_generations(self.root)
            / manifest["generation"] / repomap.ARCH_MANIFEST)
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["nodes"] = {}
        path.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaisesRegex(
                repomap.RepoMapError, "nodes"):
            repomap.load_architecture_manifest(self.root)

    def test_current_nodes_symlink_cannot_escape_generation(self):
        manifest = self.build()
        nodes = repomap.architecture_node_dir(self.root, manifest)
        outside = Path(self.tmp.name).parent / (
            Path(self.tmp.name).name + "-nodes-outside")
        outside.mkdir()
        try:
            shutil.rmtree(nodes)
            nodes.symlink_to(outside, target_is_directory=True)
            with self.assertRaisesRegex(
                    repomap.RepoMapError, "nodes.*symlink"):
                repomap.architecture_index_text(self.root)
            self.assertEqual(list(outside.iterdir()), [])
        finally:
            nodes.unlink(missing_ok=True)
            outside.rmdir()

    def test_reduced_architecture_limit_uses_same_freshness_boundary(self):
        repomap.build_architecture(
            self.root, complete=fake_complete_factory([], NODES),
            model="fake", gateway="test-gw", max_files=1)
        self.assertTrue(
            repomap.check_architecture(
                self.root, hash_check=False, max_files=1)["fresh"])
        self.assertFalse(
            repomap.check_architecture(
                self.root, hash_check=False)["fresh"])

    def test_poisoned_generation_target_fails_before_provider(self):
        generations = (
            repomap.architecture_root(self.root) / "generations")
        generations.parent.mkdir(parents=True)
        generations.write_text("not-a-directory", encoding="utf-8")
        calls = []

        def complete(*_args, **_kwargs):
            calls.append("provider")
            return "should-not-run"

        with self.assertRaisesRegex(
                repomap.RepoMapError, "generations.*目录"):
            repomap.build_architecture(
                self.root, complete=complete,
                model="fake", gateway="test-gw")
        self.assertEqual(calls, [])

    def test_poisoned_synthesis_evidence_fails_before_provider(self):
        cache = repomap._architecture_cache_dir(self.root)
        cache.mkdir(parents=True)
        (cache / "last-synthesis.txt").mkdir()
        calls = []

        def complete(*_args, **_kwargs):
            calls.append("provider")
            return "should-not-run"

        with self.assertRaisesRegex(
                repomap.RepoMapError, "synthesis evidence"):
            repomap.build_architecture(
                self.root, complete=complete,
                model="fake", gateway="test-gw")
        self.assertEqual(calls, [])


class TruncationRepair(unittest.TestCase):
    def test_truncated_synthesis_recovers_complete_prefix(self):
        """真机事故：86 文件的合成 JSON 在 max_tokens 处被砍。
        完整前缀必须救回来，而不是丢整张图重跑合成。"""
        blob = ('```json\n{"nodes": [\n'
                '{"name": "A", "type": "system", "summary": "s。",'
                ' "sources": ["alpha.py"], "links": []},\n'
                '{"name": "B", "type": "concept", "summary": "t。",'
                ' "sources": ["alpha.py"], "links": []},\n'
                '{"name": "C", "type": "file", "summary": "被截')
        nodes = repomap._parse_nodes(
            blob, valid_paths={"alpha.py"}, hashes={"alpha.py": "h"})
        self.assertEqual([n["name"] for n in nodes], ["A", "B"],
                         "完整的前两个节点应被救回")


class DriftAndIndex(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        seed_repo(self.root)
        repomap.build(self.root,
                      complete=fake_complete_factory([], NODES))

    def tearDown(self):
        self.tmp.cleanup()

    def test_check_reports_drift_with_zero_model_calls(self):
        (self.root / "alpha.py").write_text("changed = 1\n", encoding="utf-8")
        (self.root / "gamma.py").write_text("new = 1\n", encoding="utf-8")
        drift = repomap.check(self.root)
        self.assertEqual(drift["changed"], ["alpha.py"])
        self.assertEqual(drift["added"], ["gamma.py"])
        self.assertFalse(drift["fresh"])
        self.assertIn(repomap.slugify("核心系统"), drift["stale_nodes"])
        self.assertIn(repomap.slugify("设计取舍"), drift["stale_nodes"])

    def test_fresh_map_reports_fresh(self):
        self.assertTrue(repomap.check(self.root)["fresh"])

    def test_index_absent_map_is_empty(self):
        with tempfile.TemporaryDirectory() as other:
            self.assertEqual(repomap.index_text(other), "")

    def test_index_is_bounded_and_marks_stale(self):
        text = repomap.index_text(self.root)
        self.assertIn("[[" + repomap.slugify("核心系统") + "]]", text)
        self.assertIn("concept", text)
        (self.root / "alpha.py").write_text("drifted = 1\n", encoding="utf-8")
        # mtime/size 指纹要能捕捉到（同秒内 size 已变，足够）
        text2 = repomap.index_text(self.root)
        self.assertIn("stale", text2)
        clipped = repomap.index_text(self.root, max_chars=40)
        self.assertLessEqual(len(clipped), 40 + len("\n[索引截断]"))
        self.assertEqual(repomap.index_text(self.root, max_chars=0), "")


class Tier1AndDiscovery(unittest.TestCase):
    def test_wiring_extracts_symbols_and_survives_syntax_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "ok.py").write_text(
                "import json\n\ndef f():\n    pass\n\nclass C:\n    pass\n",
                encoding="utf-8")
            (root / "bad.py").write_text("def broken(:\n", encoding="utf-8")
            wiring = repomap.wiring_of(root, ["ok.py", "bad.py"])
            names={f"{x['kind']} {x['name']}" for x in wiring["ok.py"]["symbols"]}
            self.assertIn("def f", names)
            self.assertIn("class C", names)
            self.assertIn("json", {i["module"] for i in wiring["ok.py"]["imports"]})
            self.assertIn("error", wiring["bad.py"],
                          "语法错必须记录而非炸掉构建")

    def test_internal_import_edges_cover_absolute_and_relative_modules(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "pkg").mkdir()
            (root / "pkg" / "__init__.py").write_text("", encoding="utf-8")
            (root / "pkg" / "core.py").write_text(
                "def run():\n    pass\n", encoding="utf-8")
            (root / "pkg" / "worker.py").write_text(
                "from .core import run\n", encoding="utf-8")
            (root / "main.py").write_text(
                "import pkg.worker\n", encoding="utf-8")
            files = [
                "main.py", "pkg/__init__.py", "pkg/core.py", "pkg/worker.py"]
            wiring = repomap.wiring_of(root, files)
            self.assertEqual(repomap.resolve_import_edges(wiring, files), [
                ("main.py", "pkg/worker.py"),
                ("pkg/worker.py", "pkg/core.py"),
            ])

    def test_discovery_truncation_is_explicit(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for i in range(6):
                (root / f"f{i}.py").write_text("x=1\n", encoding="utf-8")
            files, notes = repomap.discover_files(root, max_files=4)
            self.assertEqual(len(files), 4)
            self.assertTrue(any("截断" in n for n in notes),
                            "静默截断会让地图假装覆盖了一切")

    def test_non_git_discovery_stops_after_bounded_prefix(self):
        """max_files 必须限制遍历本身，不能在走完整棵树后才切片。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for index in range(12):
                directory = root / f"d{index:02d}"
                directory.mkdir()
                (directory / "source.py").write_text(
                    "x = 1\n", encoding="utf-8")
            yielded = []

            def fake_walk(_root):
                for index in range(12):
                    yielded.append(index)
                    directory = root / f"d{index:02d}"
                    yield str(directory), [], ["source.py"]

            failed_git = subprocess.CompletedProcess(
                args=["git", "ls-files"], returncode=128,
                stdout="", stderr="not a repository")
            with (
                    mock.patch.object(
                        repomap.subprocess, "run", return_value=failed_git),
                    mock.patch.object(
                        repomap.os, "walk", side_effect=fake_walk),
            ):
                files, notes = repomap.discover_files(root, max_files=2)

        self.assertEqual(files, ["d00/source.py", "d01/source.py"])
        self.assertEqual(yielded, [0, 1, 2])
        self.assertTrue(any("截断" in note for note in notes))

    def test_non_git_discovery_skips_hidden_sources(self):
        """HOME fallback 不得把 dotfiles 或隐藏目录送进 map 候选。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".hidden.py").write_text("secret = 1\n", encoding="utf-8")
            (root / ".private").mkdir()
            (root / ".private" / "secret.py").write_text(
                "secret = 2\n", encoding="utf-8")
            (root / "visible.py").write_text("safe = 1\n", encoding="utf-8")
            files, _ = repomap.discover_files(root, max_files=20)

        self.assertEqual(files, ["visible.py"])

    def test_map_dir_is_never_its_own_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            seed_repo(root)
            repomap.build(root, complete=fake_complete_factory([], NODES))
            files, _ = repomap.discover_files(root)
            self.assertFalse(
                any(f.startswith(".zylab") for f in files))


class DeterministicMap(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "core.py").write_text(
            "def target():\n    return 1\n\ndef secondary():\n    return 2\n",
            encoding="utf-8")
        (self.root / "a.py").write_text(
            "from core import target\n", encoding="utf-8")
        (self.root / "b.py").write_text(
            "from core import target\n", encoding="utf-8")
        (self.root / "c.py").write_text(
            "from core import secondary\n", encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def test_map_is_deterministic_and_has_no_provider_boundary(self):
        first = repomap.build_map(self.root)
        first_bytes = (
            repomap.map_dir(self.root) / repomap.MAP_DATA).read_bytes()
        second = repomap.build_map(self.root)
        second_bytes = (
            repomap.map_dir(self.root) / repomap.MAP_DATA).read_bytes()
        self.assertEqual(first, second)
        self.assertEqual(first_bytes, second_bytes)
        self.assertEqual(first["totals"]["files"], 4)
        self.assertGreaterEqual(first["totals"]["edges"], 3)
        self.assertNotIn("model", first)
        self.assertNotIn("gateway", first)

    def test_map_ranks_hubs_by_internal_in_degree(self):
        payload = repomap.build_map(self.root)
        hotspots = payload["hotspots"]
        self.assertEqual(hotspots[0]["name"], "target")
        self.assertEqual(hotspots[0]["in_degree"], 2)
        self.assertEqual(hotspots[1]["name"], "secondary")
        self.assertEqual(hotspots[1]["in_degree"], 1)

    def test_same_size_same_integer_second_edit_is_stale(self):
        repomap.build_map(self.root)
        path = self.root / "a.py"
        old = path.stat()
        original = path.read_text(encoding="utf-8")
        changed = original.replace("target", "targex")
        self.assertEqual(len(original), len(changed))
        path.write_text(changed, encoding="utf-8")
        base = (old.st_mtime_ns // 1_000_000_000) * 1_000_000_000
        fraction = (old.st_mtime_ns - base + 1) % 1_000_000_000
        os.utime(path, ns=(old.st_atime_ns, base + fraction))
        drift = repomap.check_map(self.root, hash_check=False)
        self.assertIn("a.py", drift["changed"])
        self.assertFalse(drift["fresh"])

    def test_workspace_edit_invalidates_projection(self):
        class FakeAgent:
            def __init__(self):
                self.contexts = []

            def set_repo_context(self, repo_map, architecture):
                self.contexts.append((repo_map, architecture))

        session = object.__new__(CLI.Session)
        session.cfg = json.loads(json.dumps(settings.DEFAULTS))
        # This test exercises the legacy projection refresh contract itself;
        # /map is now default-off, so opt in locally rather than rebuilding it
        # for every ordinary session.
        session.cfg["map"]["use"] = True
        session._session_cwd = str(self.root)
        session.ag = FakeAgent()
        session.repo_map_index = ""
        session.architecture_index = ""
        session._repo_map_error = None
        CLI.Session.refresh_repo_context(session)
        path = self.root / "a.py"
        before = path.read_text(encoding="utf-8")
        path.write_text(
            before.replace("target", "targex"), encoding="utf-8")
        self.assertFalse(
            repomap.check_map(self.root, hash_check=False)["fresh"])
        CLI.Session._workspace_changed(
            session, "write_file", {"path": "a.py"})
        self.assertTrue(
            repomap.check_map(self.root, hash_check=False)["fresh"])
        self.assertGreaterEqual(len(session.ag.contexts), 2)

    def test_automatic_projection_treats_empty_workspace_as_zero_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = object.__new__(CLI.Session)
            session.cfg = json.loads(json.dumps(settings.DEFAULTS))
            session._session_cwd = tmp
            session.repo_map_index = "old"
            session.architecture_index = "old"
            session._repo_map_error = None

            class FakeAgent:
                def __init__(self):
                    self.contexts = []

                def set_repo_context(self, repo_map, architecture):
                    self.contexts.append((repo_map, architecture))

            session.ag = FakeAgent()
            result = CLI.Session.refresh_repo_context(session)
        self.assertEqual(result, {"map": "", "architecture": ""})
        self.assertEqual(session.ag.contexts[-1], ("", ""))
        self.assertIsNone(session._repo_map_error)

    def test_formatted_map_obeys_hard_character_budget(self):
        payload = repomap.build_map(self.root)
        rendered = repomap.format_map(payload, max_chars=120)
        self.assertLessEqual(len(rendered), 120)
        self.assertIn("[repo map truncated]", rendered)

    def test_reduced_file_limit_does_not_cause_perpetual_staleness(self):
        repomap.build_map(self.root, max_files=2)
        self.assertTrue(
            repomap.check_map(
                self.root, hash_check=False, max_files=2)["fresh"])
        self.assertFalse(
            repomap.check_map(
                self.root, hash_check=False)["fresh"])

    def test_duplicate_symbol_names_still_obey_global_node_cap(self):
        files = [f"module_{index:02d}.py" for index in range(20)]
        repeated = [
            {"kind": "def", "name": "same", "line": line}
            for line in range(1, 401)]
        wiring = {
            rel: {"symbols": repeated, "imports": []}
            for rel in files}
        graph = repomap._graph_from_wiring(wiring, files)
        self.assertLessEqual(
            len(graph["nodes"]),
            len(files) + repomap.MAX_MAP_SYMBOLS)
        self.assertGreater(graph["dropped_symbols"], 0)

    def test_generated_cache_uses_local_exclude_not_tracked_gitignore(self):
        subprocess.run(
            ["git", "init", "-q"], cwd=self.root, check=True)
        gitignore = self.root / ".gitignore"
        gitignore.write_text("keep-this-line\n", encoding="utf-8")
        repomap.build_map(self.root)
        self.assertEqual(
            gitignore.read_text(encoding="utf-8"),
            "keep-this-line\n")
        exclude = self.root / ".git" / "info" / "exclude"
        self.assertIn(
            repomap.MAP_DIRNAME + "/",
            exclude.read_text(encoding="utf-8").splitlines())
        status = subprocess.run(
            ["git", "status", "--porcelain", "--", ".zylab"],
            cwd=self.root, capture_output=True, text=True, check=True)
        self.assertEqual(status.stdout, "")

    def test_local_exclude_symlink_cannot_escape_git_directory(self):
        subprocess.run(
            ["git", "init", "-q"], cwd=self.root, check=True)
        info = self.root / ".git" / "info"
        outside = Path(self.tmp.name).parent / (
            Path(self.tmp.name).name + "-git-info-outside")
        outside.mkdir()
        try:
            shutil.rmtree(info)
            info.symlink_to(outside, target_is_directory=True)
            repomap.build_map(self.root)
            self.assertEqual(list(outside.iterdir()), [])
        finally:
            if info.is_symlink():
                info.unlink()
            if outside.exists():
                outside.rmdir()

    def test_tampered_map_fails_closed_with_domain_error(self):
        repomap.build_map(self.root)
        path = repomap.map_dir(self.root) / repomap.MAP_DATA
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["graph"]["nodes"] = 7
        path.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaisesRegex(
                repomap.RepoMapError, "nodes"):
            repomap.load_map(self.root)

    def test_falsey_wrong_map_collection_type_fails_closed(self):
        repomap.build_map(self.root)
        path = repomap.map_dir(self.root) / repomap.MAP_DATA
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["graph"]["nodes"] = {}
        path.write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaisesRegex(
                repomap.RepoMapError, "nodes"):
            repomap.load_map(self.root)

    def test_visualization_export_replaces_symlink_not_target(self):
        repomap.build_map(self.root)
        outside = Path(self.tmp.name).parent / (
            Path(self.tmp.name).name + "-map-viz-outside")
        outside.write_text("OUTSIDE-SENTINEL", encoding="utf-8")
        link = repomap.map_dir(self.root) / "map.html"
        try:
            link.symlink_to(outside)
            html_path, _ = repomap.export_visualizations(
                self.root, kind="map", html="<html>safe</html>",
                mermaid="flowchart LR\n")
            self.assertFalse(html_path.is_symlink())
            self.assertEqual(
                outside.read_text(encoding="utf-8"),
                "OUTSIDE-SENTINEL")
        finally:
            outside.unlink(missing_ok=True)


class MapCli(unittest.TestCase):
    class Session:
        def __init__(self, root):
            self._session_cwd = str(root)
            self.cfg = json.loads(json.dumps(settings.DEFAULTS))
            self.repo_map_index = ""
            self.architecture_index = ""

        def refresh_repo_context(self):
            self.repo_map_index = repomap.map_index_text(
                self._session_cwd)
            return {"map": self.repo_map_index, "architecture": ""}

    def test_map_build_is_zero_provider_and_architecture_is_separate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            seed_repo(root)
            session = self.Session(root)
            with (
                    mock.patch.object(
                        CLI.client, "stream_chat",
                        side_effect=AssertionError("provider must stay closed")),
                    contextlib.redirect_stdout(io.StringIO()) as output,
            ):
                CLI.cmd_map(session, "build")
        self.assertIn("0 provider requests", output.getvalue())
        self.assertIn("architecture", CLI.REGISTRY)
        self.assertIs(CLI.REGISTRY["architecture"][0], CLI.cmd_architecture)

    def test_cli_rejects_unknown_map_arguments(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = self.Session(Path(tmp))
            with (
                    mock.patch.object(
                        CLI.REPOMAP, "build_map",
                        side_effect=AssertionError("invalid args must not run")),
                    contextlib.redirect_stdout(io.StringIO()) as output,
            ):
                CLI.cmd_map(session, "build --deep")
        self.assertIn("未知参数", output.getvalue())
        self.assertIn("/architecture", output.getvalue())


class ArchitectureCli(unittest.TestCase):
    class Agent:
        model = "fake-model"
        gateway = "test-gw"
        session_id = "session-test"
        metrics = None

    class Session:
        def __init__(self, root):
            self._session_cwd = str(root)
            self.cfg = json.loads(json.dumps(settings.DEFAULTS))
            self.ag = ArchitectureCli.Agent()
            self.refreshes = 0

        def route_selection(self):
            return self.ag.model, self.ag.gateway

        def refresh_repo_context(self):
            self.refreshes += 1

    def test_build_preflights_pins_route_and_traces_each_purpose(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            seed_repo(root)
            session = self.Session(root)
            calls = []

            def stream(model, messages, **kwargs):
                system = messages[0]["content"]
                calls.append({
                    "model": model,
                    "gateway": kwargs.get("gateway"),
                    "trace": kwargs.get("trace_context") or {},
                })
                if "架构图" in system:
                    value = json.dumps(NODES, ensure_ascii=False)
                else:
                    value = "稳定摘要。包含一个可验证的不变量。"
                yield {"t": "text", "v": value}

            with (
                    mock.patch.object(
                        CLI.client, "stream_chat", side_effect=stream),
                    contextlib.redirect_stdout(io.StringIO()) as output,
            ):
                CLI.cmd_architecture(session, "build")

        rendered = output.getvalue()
        self.assertIn("architecture 预检", rendered)
        self.assertIn("预计上限 3 provider requests", rendered)
        self.assertIn("generation ", rendered)
        self.assertEqual(session.refreshes, 1)
        self.assertEqual(len(calls), 3)
        self.assertEqual(
            {item["gateway"] for item in calls}, {"test-gw"})
        purposes = [
            item["trace"]["raw"]["purpose"] for item in calls]
        self.assertEqual(
            purposes,
            ["architecture_summary", "architecture_summary",
             "architecture_synthesis"])
        self.assertTrue(all(
            item["trace"]["raw"]["projection"] == "architecture"
            for item in calls))

    def test_unknown_argument_rejects_without_provider(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = self.Session(Path(tmp))
            with (
                    mock.patch.object(
                        CLI.client, "stream_chat",
                        side_effect=AssertionError(
                            "invalid args must not call provider")),
                    contextlib.redirect_stdout(io.StringIO()) as output,
            ):
                CLI.cmd_architecture(session, "build --deep")
        self.assertIn("未知参数", output.getvalue())
        self.assertIn("只有 build 会调用 provider", output.getvalue())

    def test_progress_is_visible_in_a_real_pty(self):
        body = r"""
import json
import os
import tempfile

with tempfile.TemporaryDirectory(prefix="zylab-archpty-") as home:
    os.environ["HOME"] = home
    import zylab
    progress = zylab.ArchitectureBuildProgress()
    with progress:
        progress({
            "stage": "preflight", "cache_hits": 1, "cache_misses": 2,
            "estimated_requests": 3,
        })
        progress({
            "stage": "summary", "state": "result",
            "completed": 1, "total": 2, "file": "alpha.py",
        })
    print("RESULT:" + json.dumps({"tty": zylab._TTY}))
"""
        output, result = run_pty_child(
            body, [], cwd=str(Path(__file__).resolve().parents[1]),
            timeout=8.0)
        self.assertTrue(result["tty"])
        self.assertIn("architecture 预检", output)
        self.assertIn("摘要 alpha.py", output)

    def test_managed_cancel_preserves_queued_prompt_without_stdin_race(self):
        class Event:
            def __init__(self, kind, value=None):
                self.kind = kind
                self.value = value

        class Pump:
            def __init__(self):
                self.events = [
                    Event("redraw"),
                    Event("line", {"text": "queued research prompt"}),
                    Event("interrupt"),
                ]

            def get(self, timeout):
                if self.events:
                    return self.events.pop(0)
                time.sleep(min(float(timeout or 0), 0.01))
                return None

        class Session:
            pump = Pump()

            def __init__(self):
                self._deferred_pump_events = []

        session = Session()
        with CLI.ArchitectureCancelWatcher(session) as watcher:
            deadline = time.monotonic() + 1.0
            while not watcher.event.is_set() and time.monotonic() < deadline:
                time.sleep(0.005)
            self.assertTrue(watcher.event.is_set())
        self.assertEqual(
            [event.kind for event in session._deferred_pump_events],
            ["line"])
        self.assertEqual(
            session._deferred_pump_events[0].value["text"],
            "queued research prompt")


class Visualization(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        seed_repo(self.root)
        repomap.build(self.root,
                      complete=fake_complete_factory([], NODES))
        self.manifest = repomap.load_manifest(self.root)

    def tearDown(self):
        self.tmp.cleanup()

    def test_html_is_self_contained_with_all_nodes(self):
        """零外链是硬约束：viz 要在无网环境 file:// 直开。"""
        html = repomap.render_viz_html(self.manifest)
        self.assertNotIn("http://", html)
        self.assertNotIn("https://", html)
        self.assertNotIn("<script src", html)
        for node in self.manifest["nodes"]:
            self.assertIn(node["slug"], html)
        self.assertIn("validates", html, "边数据应嵌入")

    def test_html_marks_stale_nodes(self):
        (self.root / "alpha.py").write_text("drift=1\n", encoding="utf-8")
        drift = repomap.check(self.root)
        html = repomap.render_viz_html(self.manifest, drift)
        self.assertIn('"stale": true', html)

    def test_mermaid_shape(self):
        mmd = repomap.render_mermaid(self.manifest)
        self.assertTrue(mmd.startswith("flowchart LR"))
        self.assertIn("-- validates -->", mmd)
        for node in self.manifest["nodes"]:
            self.assertIn(node["slug"], mmd)


class PhaseASecurity(unittest.TestCase):
    """REVIEW-repo-map-20260828 Phase A 的回归钉：F1/F3/F4。

    每条都先在真机复现过（凭据内容进 provider 消息、</script> 逃逸、
    空文件幻觉被永久缓存），不是理论风险。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _spy_build(self, model="fake", nodes=None, reply=None):
        seen = []

        def complete(messages, max_tokens=None):
            seen.append(messages[1]["content"])
            if "架构图" in messages[0]["content"]:
                return json.dumps(nodes or NODES, ensure_ascii=False)
            if reply:
                return reply(messages[1]["content"])
            return "正常摘要。有内容。"
        manifest = repomap.build(self.root, complete=complete, model=model,
                                 gateway="test-gw")
        return manifest, seen

    def test_sensitive_files_never_reach_provider(self):
        """F1：git 跟踪 ≠ 允许送外部模型。"""
        seed_repo(self.root)
        (self.root / "credentials.json").write_text(
            '{"aws":"AKIA-SENTINEL"}', encoding="utf-8")
        (self.root / ".env").write_text("T=sk-SENTINEL2\n", encoding="utf-8")
        (self.root / "server.pem").write_text("SENTINEL3", encoding="utf-8")
        manifest, seen = self._spy_build()
        blob = "\n".join(seen)
        for sentinel in ("AKIA-SENTINEL", "sk-SENTINEL2", "SENTINEL3"):
            self.assertNotIn(sentinel, blob)
        self.assertNotIn("credentials.json", manifest["files"])
        self.assertTrue(any("排除" in n for n in manifest.get("notes", [])),
                        "排除必须显式计数，不能静默")

    def test_git_ignored_text_never_reaches_provider(self):
        subprocess.run(
            ["git", "init", "-q"], cwd=self.root, check=True)
        (self.root / ".gitignore").write_text(
            "ignored-notes.txt\n", encoding="utf-8")
        (self.root / "visible.py").write_text(
            "value = 1\n", encoding="utf-8")
        (self.root / "ignored-notes.txt").write_text(
            "IGNORED-SENTINEL", encoding="utf-8")
        nodes = {"nodes": [{
            "name": "visible", "type": "system",
            "summary": "visible", "sources": ["visible.py"],
            "links": []}]}
        manifest, seen = self._spy_build(nodes=nodes)
        self.assertNotIn("IGNORED-SENTINEL", "\n".join(seen))
        self.assertNotIn("ignored-notes.txt", manifest["files"])

    def test_zylab_internal_state_never_reaches_provider(self):
        subprocess.run(
            ["git", "init", "-q"], cwd=self.root, check=True)
        (self.root / "visible.py").write_text(
            "value = 1\n", encoding="utf-8")
        internal = self.root / ".zylab"
        internal.mkdir()
        (internal / "settings.json").write_text(
            '{"api_key":"ZYLAB-INTERNAL-SENTINEL"}',
            encoding="utf-8")
        nodes = {"nodes": [{
            "name": "visible", "type": "system",
            "summary": "visible", "sources": ["visible.py"],
            "links": []}]}
        manifest, seen = self._spy_build(nodes=nodes)
        self.assertNotIn(
            "ZYLAB-INTERNAL-SENTINEL", "\n".join(seen))
        self.assertNotIn(".zylab/settings.json", manifest["files"])

    def test_symlink_source_never_reaches_provider(self):
        outside = Path(self.tmp.name).parent / (
            Path(self.tmp.name).name + "-outside.py")
        outside.write_text(
            "value = 'SYMLINK-SENTINEL'\n", encoding="utf-8")
        try:
            (self.root / "visible.py").write_text(
                "value = 1\n", encoding="utf-8")
            (self.root / "linked.py").symlink_to(outside)
            nodes = {"nodes": [{
                "name": "visible", "type": "system",
                "summary": "visible", "sources": ["visible.py"],
                "links": []}]}
            manifest, seen = self._spy_build(nodes=nodes)
            self.assertNotIn("SYMLINK-SENTINEL", "\n".join(seen))
            self.assertNotIn("linked.py", manifest["files"])
        finally:
            outside.unlink(missing_ok=True)

    def test_map_authority_symlink_escape_is_rejected(self):
        outside = Path(self.tmp.name).parent / (
            Path(self.tmp.name).name + "-map-outside")
        outside.mkdir()
        try:
            (self.root / ".zylab").symlink_to(
                outside, target_is_directory=True)
            (self.root / "visible.py").write_text(
                "value = 1\n", encoding="utf-8")
            with self.assertRaisesRegex(
                    repomap.RepoMapError, "symlink"):
                repomap.build_map(self.root)
            self.assertEqual(list(outside.iterdir()), [])
        finally:
            outside.rmdir()

    def test_secret_lines_redacted_in_summary_input(self):
        """F1 第二道网：白名单文件内嵌的 secret 赋值也要遮。"""
        (self.root / "config.py").write_text(
            "api_key = 'sk-INLINE-SENTINEL'\nx = 1\n", encoding="utf-8")
        _, seen = self._spy_build(
            nodes={"nodes": [{"name": "n", "type": "system",
                              "summary": "s", "sources": ["config.py"],
                              "links": []}]})
        blob = "\n".join(seen)
        self.assertNotIn("sk-INLINE-SENTINEL", blob)
        self.assertIn("[REDACTED]", blob)
        self.assertIn("x = 1", blob, "非 secret 行不得误伤")

    def test_cache_key_includes_generation_provenance(self):
        """F4：换模型不得复用旧摘要；同模型命中缓存。"""
        seed_repo(self.root)
        counts = []
        for model in ("model-A", "model-B", "model-A"):
            _, seen = self._spy_build(model=model)
            counts.append(sum("File:" in c for c in seen))
        self.assertEqual(counts, [2, 2, 0],
                         "A 首建 2 次、换 B 重摘 2 次、回 A 全命中")

    def test_empty_file_gets_deterministic_summary_without_provider(self):
        """F4：真实事故 —— 0 字节文件曾获得整段幻觉并被永久缓存。"""
        seed_repo(self.root)
        (self.root / "empty.py").write_text("", encoding="utf-8")
        _, seen = self._spy_build()
        self.assertFalse(any("File: empty.py" in c for c in seen),
                         "空文件不得发起摘要调用（合成 digest 里出现名字是正常的）")

    def test_degenerate_summary_falls_back_and_is_not_cached(self):
        """F4：复读机产出降级为结构描述，且不进缓存（下次重试）。"""
        seed_repo(self.root)

        def junk(user):
            return ('"nodes": { ' * 40 if "beta.py" in user else "好摘要。")
        _, seen1 = self._spy_build(reply=junk)
        synth1 = next(c for c in seen1 if "### beta.py" in c)
        self.assertIn("（模型摘要不可用", synth1)
        _, seen2 = self._spy_build()
        self.assertTrue(any("File: beta.py" in c for c in seen2),
                        "退化产出不得被缓存固化")

    def test_viz_escapes_script_breakout(self):
        """F3：仓库/模型文本不得打断 script 或进入 innerHTML。"""
        hostile = {"built_at": "t", "model": "m", "nodes": [{
            "name": 'x</script><script>alert(1)</script>', "slug": "x",
            "type": "system", "summary": '</script><script>alert(2)',
            "sources": [{"path": "a</b>.py", "hash": "h"}], "links": []}]}
        html = repomap.render_viz_html(hostile)
        self.assertNotIn("</script><script>alert", html)
        self.assertNotIn("panel.innerHTML", html)
        self.assertIn('type="application/json"', html)

    def test_mermaid_label_metachars_sanitized(self):
        mmd = repomap.render_mermaid({"nodes": [{
            "name": 'a["x"]|b<i>', "slug": "a", "type": "system",
            "links": []}]})
        body = mmd.splitlines()[1]
        for ch in '[]{}()|"<>`':
            self.assertNotIn(ch, body.split("a[", 1)[1].rstrip("]"),
                             f"标签中的 {ch!r} 未消毒")


class Defaults(unittest.TestCase):
    def test_map_policy_default_pinned(self):
        """AGENTS.md §1：配置默认值必须有测试钉住。"""
        policy = settings.map_policy(settings.DEFAULTS)
        # Legacy /map remains an explicit compatibility command, but its
        # projection no longer consumes every turn's context by default.
        self.assertEqual(policy["use"], False)
        self.assertEqual(policy["use_architecture"], False)
        self.assertEqual(policy["max_index_chars"], repomap.MAX_INDEX_CHARS)
        self.assertEqual(
            policy["max_architecture_chars"], repomap.MAX_INDEX_CHARS)
        self.assertEqual(policy["max_files"], repomap.MAX_FILES)
        self.assertEqual(policy["max_dirs"], repomap.DEFAULT_MAX_DIRS)
        self.assertEqual(
            policy["hubs_per_dir"], repomap.DEFAULT_HUBS_PER_DIR)
        self.assertEqual(
            policy["hotspots"], repomap.DEFAULT_HOTSPOTS)
        self.assertLessEqual(
            policy["max_index_chars"], repomap.MAX_INDEX_CHARS_HARD)

    def test_project_map_config_can_only_reduce_hard_bounds(self):
        cfg = json.loads(json.dumps(settings.DEFAULTS))
        settings._merge(cfg, {"map": {
            "max_index_chars": 11_000,
            "max_files": 399,
            "max_dirs": 15,
            "use": False,
        }}, allow_permission_relax=False)
        policy = settings.map_policy(cfg)
        self.assertEqual(policy["max_index_chars"], 6_000)
        self.assertEqual(policy["max_files"], 399)
        self.assertEqual(policy["max_dirs"], 15)
        self.assertFalse(policy["use"])


if __name__ == "__main__":
    unittest.main()
