"""Managed optional Graft component manifest and repair tests."""
import hashlib
import json
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path

from core import graft_component


def _sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class ManagedGraftComponentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.persistent = Path(self.temp.name)
        self.app = self.persistent / "zylab"
        self.app.mkdir()

        self.node = self.persistent / "tools" / "node-v24.19.0" / "bin" / "node"
        self.node.parent.mkdir(parents=True)
        self.node.write_text("#!/bin/sh\nprintf 'v24.19.0\\n'\n", encoding="utf-8")
        self.node.chmod(0o755)

        self.repo = self.persistent / "Graft"
        (self.repo / "dist").mkdir(parents=True)
        (self.repo / "package-lock.json").write_text(
            '{"lockfileVersion": 3}\n', encoding="utf-8")
        (self.repo / "dist" / "cli.js").write_text(
            "#!/usr/bin/env node\nconsole.log('graft');\n", encoding="utf-8")
        self.dependencies = self.repo / "node_modules"
        (self.dependencies / "example").mkdir(parents=True)
        (self.dependencies / ".package-lock.json").write_text(
            '{"installed": true}\n', encoding="utf-8")
        (self.dependencies / "example" / "index.js").write_text(
            "export const value = 1;\n", encoding="utf-8")
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        subprocess.run(
            ["git", "-C", str(self.repo), "add", "package-lock.json", "dist/cli.js"],
            check=True)
        subprocess.run(
            ["git", "-C", str(self.repo), "-c", "user.name=Test",
             "-c", "user.email=test@example.invalid", "commit", "-qm", "fixture"],
            check=True)
        head = subprocess.run(
            ["git", "-C", str(self.repo), "rev-parse", "HEAD"],
            check=True, capture_output=True, text=True).stdout.strip()
        tree = graft_component.tree_digest(self.dependencies)

        self.launcher = self.persistent / ".local" / "bin" / "graft"
        self.lock = self.app / "graft-component.lock.json"
        self.lock.write_text(json.dumps({
            "schema_version": 1,
            "component": "graft-hosted",
            "required": False,
            "node": {
                "path": "../tools/node-v24.19.0/bin/node",
                "version": "v24.19.0",
                "sha256": _sha256(self.node),
            },
            "graft": {
                "path": "../Graft",
                "revision": head,
                "upstream_revision": head,
                "package_lock_sha256": _sha256(self.repo / "package-lock.json"),
                "cli": "dist/cli.js",
                "cli_sha256": _sha256(self.repo / "dist" / "cli.js"),
                "dependencies": {
                    "path": "node_modules",
                    "installed_lock": "node_modules/.package-lock.json",
                    "installed_lock_sha256": _sha256(
                        self.dependencies / ".package-lock.json"),
                    "tree_algorithm": "sha256-tree-v2",
                    "tree_sha256": tree["sha256"],
                    "entries": tree["entries"],
                    "bytes": tree["bytes"],
                },
            },
            "launcher": {"path": "../.local/bin/graft"},
        }), encoding="utf-8")

    def tearDown(self):
        self.temp.cleanup()

    def test_missing_launcher_is_repairable_without_network(self):
        before = graft_component.inspect(self.lock)
        self.assertFalse(before["ready"])
        self.assertTrue(before["repairable"])
        self.assertFalse(before["full_verified"])
        self.assertFalse(before["checks"]["launcher"]["ok"])

        after = graft_component.repair_launcher(self.lock)
        self.assertTrue(after["ready"])
        self.assertTrue(after["full_verified"])
        self.assertEqual(stat.S_IMODE(self.launcher.stat().st_mode), 0o755)
        text = self.launcher.read_text(encoding="utf-8")
        self.assertIn(str(self.node), text)
        self.assertIn(str(self.repo / "dist" / "cli.js"), text)

    def test_tampered_runtime_is_not_repaired_or_executed(self):
        self.node.write_text("#!/bin/sh\nprintf 'v0-evil\\n'\n", encoding="utf-8")
        report = graft_component.inspect(self.lock)
        self.assertFalse(report["checks"]["node_sha256"]["ok"])
        self.assertFalse(report["checks"]["node_version"]["ok"])
        self.assertFalse(report["repairable"])
        with self.assertRaisesRegex(graft_component.ComponentError, "前置校验"):
            graft_component.repair_launcher(self.lock)
        self.assertFalse(self.launcher.exists())

    def test_revision_drift_is_visible(self):
        data = json.loads(self.lock.read_text(encoding="utf-8"))
        data["graft"]["revision"] = "0" * 40
        self.lock.write_text(json.dumps(data), encoding="utf-8")
        report = graft_component.inspect(self.lock)
        self.assertFalse(report["checks"]["graft_revision"]["ok"])
        self.assertIn("expected", report["checks"]["graft_revision"]["detail"])
        self.assertFalse(report["repairable"])

    def test_dependency_tree_tamper_blocks_full_check_and_repair(self):
        (self.dependencies / "example" / "index.js").write_text(
            "export const value = 'tampered';\n", encoding="utf-8")
        quick = graft_component.inspect(self.lock)
        self.assertTrue(quick["repairable"])
        self.assertFalse(quick["full_verified"])
        full = graft_component.inspect(self.lock, full=True)
        self.assertFalse(full["checks"]["dependencies_tree"]["ok"])
        self.assertFalse(full["repairable"])
        with self.assertRaisesRegex(graft_component.ComponentError, "前置校验"):
            graft_component.repair_launcher(self.lock)

    def test_manifest_paths_cannot_escape_persistent_root(self):
        data = json.loads(self.lock.read_text(encoding="utf-8"))
        data["launcher"]["path"] = "../../outside/graft"
        self.lock.write_text(json.dumps(data), encoding="utf-8")
        with self.assertRaisesRegex(graft_component.ComponentError, "persistent root"):
            graft_component.inspect(self.lock)

    def test_missing_manifest_is_a_component_error(self):
        with self.assertRaisesRegex(graft_component.ComponentError, "无法读取"):
            graft_component.inspect(self.app / "missing.lock.json")

    def test_launcher_symlink_is_never_followed_during_repair(self):
        target = self.persistent / "other-launcher"
        target.write_text("do-not-overwrite\n", encoding="utf-8")
        self.launcher.parent.mkdir(parents=True)
        self.launcher.symlink_to(target)
        report = graft_component.inspect(self.lock)
        self.assertTrue(report["repairable"])
        with self.assertRaisesRegex(graft_component.ComponentError, "symlink"):
            graft_component.repair_launcher(self.lock)
        self.assertEqual(
            target.read_text(encoding="utf-8"), "do-not-overwrite\n")


if __name__ == "__main__":
    unittest.main()
