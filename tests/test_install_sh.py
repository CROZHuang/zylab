"""§2.5 install.sh：薄 stub（普通文件，非 symlink），幂等，可卸载；从别的目录跑 stub 能起入口。"""
import os
import stat
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def sh(args, **kw):
    return subprocess.run(["bash", os.path.join(ROOT, "install.sh"), *args], cwd=ROOT,
                          capture_output=True, text=True, timeout=60, **kw)


class InstallShTests(unittest.TestCase):
    def test_stub_is_a_plain_idempotent_file_that_runs_the_entry(self):
        with tempfile.TemporaryDirectory() as tmp:
            bin_dir = os.path.join(tmp, "bin")
            p = sh(["--bin-dir", bin_dir])
            self.assertEqual(p.returncode, 0, p.stderr)
            self.assertIn("已写入", p.stdout)
            stub = os.path.join(bin_dir, "zylab")
            self.assertTrue(os.path.isfile(stub) and not os.path.islink(stub))
            self.assertTrue(os.stat(stub).st_mode & stat.S_IXUSR)
            with open(stub, encoding="utf-8") as f:
                body = f.read()
            self.assertIn(f'REPO = "{ROOT}"', body)
            self.assertLess(body.count("\n"), 12, "薄 stub，不是副本")
            p2 = sh(["--bin-dir", bin_dir])
            self.assertIn("已是最新", p2.stdout)
            env = {"HOME": tmp, "PATH": "/usr/bin:/bin", "TERM": "dumb", "ZYLAB_APP_ROOT": tmp,
                   "ZYLAB_KEYS_FILE": os.path.join(tmp, "keys.env"), "PYTHONIOENCODING": "utf-8"}
            run = subprocess.run([sys.executable, stub, "--help"], cwd=tmp, env=env,
                                 capture_output=True, text=True, timeout=60, stdin=subprocess.DEVNULL)
            self.assertEqual(run.returncode, 0, run.stderr[-500:])
            self.assertIn("usage: zylab", run.stdout)
            p3 = sh(["--bin-dir", bin_dir, "--uninstall"])
            self.assertIn("已删除", p3.stdout)
            self.assertFalse(os.path.exists(stub))


if __name__ == "__main__":
    unittest.main()
