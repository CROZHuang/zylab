"""自包含（便携）模式：状态目录三级优先、整体搬家改写路径、拷走后自动改路径、init --portable 收编。"""
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from core import homemigrate, paths  # noqa: E402


class PrecedenceTests(unittest.TestCase):
    def test_env_then_portable_then_default(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.dict(os.environ, {}, clear=False):
            os.environ["ZYLAB_APP_ROOT"] = tmp
            os.environ.pop("ZYLAB_HOME", None)
            self.assertEqual(paths.state_home(), paths.default_home())
            self.assertFalse(paths.is_portable())
            (Path(tmp) / ".zylab-home").mkdir()
            self.assertEqual(paths.state_home(), Path(tmp) / ".zylab-home")
            self.assertTrue(paths.is_portable())
            os.environ["ZYLAB_HOME"] = os.path.join(tmp, "explicit")
            self.assertEqual(paths.state_home(), Path(tmp) / "explicit")
            self.assertFalse(paths.is_portable())

    def test_homemigrate_agrees_with_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {"HOME": tmp, "ZYLAB_APP_ROOT": tmp}
            self.assertEqual(homemigrate.current_home(env), Path(tmp) / ".zylab")
            (Path(tmp) / ".zylab-home").mkdir()
            self.assertEqual(homemigrate.current_home(env), Path(tmp) / ".zylab-home")


class RelocateAndReconcileTests(unittest.TestCase):
    def test_relocate_moves_rewrites_and_records_location(self):
        with tempfile.TemporaryDirectory() as tmp:
            old = Path(tmp) / "old"
            (old / "sessions").mkdir(parents=True)
            (old / "sessions" / "s.json").write_text(json.dumps({"p": f"{old}/x"}), encoding="utf-8")
            new = Path(tmp) / "app" / ".zylab-home"
            out = io.StringIO()
            self.assertEqual(homemigrate.relocate(old, new, out=out, verb="已收编"), 0)
            self.assertEqual(json.loads((new / "sessions" / "s.json").read_text())["p"], f"{new}/x")
            self.assertEqual((new / ".location").read_text().strip(), str(new))
            self.assertIn("已收编", out.getvalue())
            self.assertFalse(old.exists())

    def test_reconcile_after_the_folder_was_copied_elsewhere(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "moved" / ".zylab-home"
            (home / "checkpoints").mkdir(parents=True)
            (home / ".location").write_text("/somewhere/old/.zylab-home\n", encoding="utf-8")
            (home / "checkpoints" / "c.json").write_text(
                json.dumps({"file": "/somewhere/old/.zylab-home/checkpoints/blob", "keep": "/somewhere/older"}),
                encoding="utf-8")
            out = io.StringIO()
            self.assertEqual(homemigrate.reconcile_location(home, out=out), 1)
            rec = json.loads((home / "checkpoints" / "c.json").read_text())
            self.assertEqual(rec["file"], f"{home}/checkpoints/blob")
            self.assertEqual(rec["keep"], "/somewhere/older", "只改前缀完整匹配的路径")
            self.assertEqual((home / ".location").read_text().strip(), str(home))
            self.assertEqual(homemigrate.reconcile_location(home, out=io.StringIO()), 0, "第二次无事可做")


class ImportSafetyTests(unittest.TestCase):
    def test_importing_the_entry_module_touches_no_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(os.environ, HOME=tmp, PYTHONPATH=ROOT, ZYLAB_APP_ROOT=tmp)
            env.pop("ZYLAB_HOME", None)
            p = subprocess.run([sys.executable, "-c", "import zylab"], env=env, cwd=ROOT,
                               capture_output=True, text=True, timeout=90)
            self.assertEqual(p.returncode, 0, p.stderr[-500:])
            self.assertEqual(sorted(os.listdir(tmp)), [], "import 不许在家目录里建任何东西")


class InitPortableTests(unittest.TestCase):
    def test_init_portable_adopts_the_default_home_and_later_runs_use_it(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = Path(tmp) / "app"
            app.mkdir()
            default = Path(tmp) / ".zylab"
            (default / "sessions").mkdir(parents=True)
            (default / "sessions" / "s1.json").write_text(json.dumps({"id": "s1", "p": f"{default}/x"}), encoding="utf-8")
            env = {"HOME": tmp, "PATH": "/usr/bin:/bin", "TERM": "dumb", "PYTHONIOENCODING": "utf-8",
                   "ZYLAB_APP_ROOT": str(app), "ZYLAB_KEYS_FILE": os.path.join(tmp, "keys.env")}
            p = subprocess.run([sys.executable, os.path.join(ROOT, "zylab.py"), "init", "--portable", "--yes"],
                               env=env, cwd=ROOT, capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL)
            self.assertNotIn("Traceback", p.stderr, p.stderr[-800:])
            self.assertIn("已收编", p.stdout)
            self.assertEqual(p.returncode, 2, "收编之后继续 init：没 key → 2")
            portable = app / ".zylab-home"
            self.assertTrue((portable / "sessions" / "s1.json").is_file())
            self.assertFalse(default.exists())
            self.assertEqual(json.loads((portable / "sessions" / "s1.json").read_text())["p"], f"{portable}/x")
            self.assertIn("自包含", p.stdout)
            # 之后的运行（不带 --portable）用的就是应用目录里的状态
            q = subprocess.run([sys.executable, os.path.join(ROOT, "zylab.py"), "init", "--yes"],
                               env=env, cwd=ROOT, capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL)
            self.assertIn(str(portable), q.stdout)
            self.assertFalse(default.exists(), "默认目录不该被重建")


if __name__ == "__main__":
    unittest.main()
