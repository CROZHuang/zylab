"""冷启动验收（BACKLOG-rename-zylab §2.0.4）：把入口放进一个"别人的 pod"里，失败必须自己说人话。

子进程用 env -i 语义（只有 HOME/PATH/TERM），ZYLAB_KEYS_FILE 指向不存在的文件 ——
否则 client 的候选列表里有维护者机器的绝对路径，本机永远"有 key"。
"""
import ast
import contextlib
import glob
import io
import os
import stat
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENTRY = os.path.join(ROOT, "zylab.py")
sys.path.insert(0, ROOT)
import zylab  # noqa: E402


def run(args, home, extra=None, timeout=90):
    env = {"HOME": home, "PATH": "/usr/bin:/bin", "TERM": "dumb",
           "PYTHONIOENCODING": "utf-8",
           "ZYLAB_APP_ROOT": home,            # 别让仓库里的 .zylab-home/ 把测试引到真实状态
           "ZYLAB_KEYS_FILE": os.path.join(home, "keys.env")}
    env.update(extra or {})
    return subprocess.run(
        [sys.executable, ENTRY, *args], env=env, cwd=ROOT, text=True,
        capture_output=True, timeout=timeout, stdin=subprocess.DEVNULL)


class ColdStartTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="zylab-cold-")
        self.home = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def test_help_exits_zero_without_traceback(self):
        p = run(["--help"], self.home)
        self.assertEqual(p.returncode, 0, p.stderr[-800:])
        self.assertNotIn("Traceback", p.stderr)
        self.assertIn("init", p.stdout)

    def test_version_answers_without_key_and_names_a_commit(self):
        """同事报 bug 时要能一句话说清跑的是哪一版（2026-09-15 之前 --version
        直接 argparse 报错退出 2，仓库还 0 个 tag）。"""
        p = run(["--version"], self.home)
        self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
        self.assertNotIn("Traceback", p.stderr)
        self.assertRegex(p.stdout.strip(), r"^zylab (unknown|[0-9a-f]{12})")
        self.assertIn("python ", p.stdout)

    def test_tarball_version_stamp_stays_wired(self):
        """git archive 靠 .gitattributes 的 export-subst 把提交号烤进 _build.py；
        克隆出来的工作树里它必须还是占位符，否则 tarball 会报错版本。"""
        attrs = os.path.join(ROOT, ".gitattributes")
        self.assertTrue(os.path.isfile(attrs))
        with open(attrs, encoding="utf-8") as handle:
            self.assertIn("core/_build.py export-subst", handle.read())
        with open(os.path.join(ROOT, "core", "_build.py"),
                  encoding="utf-8") as handle:
            body = handle.read()
        self.assertIn("$Format:%H$", body)
        self.assertIn("$Format:%cI$", body)

    def test_init_without_key_fails_with_the_next_command(self):
        p = run(["init", "--gateway", "deepinfer", "--base", "https://127.0.0.1:9/v1",
                 "--key-env", "FAKE_KEY", "--yes"], self.home)
        self.assertEqual(p.returncode, 2, p.stdout + p.stderr)
        self.assertIn("export FAKE_KEY=", p.stdout)
        self.assertNotIn("Traceback", p.stderr)

    def test_init_writes_key_privately_and_reads_unreachable_gateway_as_not_a_key_problem(self):
        p = run(["init", "--gateway", "deepinfer", "--key-env", "FAKE_KEY", "--yes"], self.home,
                extra={"FAKE_KEY": "sk-not-a-real-key", "ZYLAB_BASE": "https://127.0.0.1:9"})
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        self.assertIn("不可达", p.stdout)
        self.assertIn("与 key 无关", p.stdout)
        self.assertNotIn("sk-not-a-real-key", p.stdout + p.stderr, "key 永不回显")
        keys = os.path.join(self.home, "keys.env")
        self.assertEqual(stat.S_IMODE(os.stat(keys).st_mode), 0o600)
        with open(keys, encoding="utf-8") as f:
            self.assertEqual(f.read(), "DEEPINFER_API_KEY=sk-not-a-real-key\n")

    def test_print_mode_without_key_says_so_within_the_temp_home(self):
        p = run(["-p", "hi"], self.home)
        self.assertEqual(p.returncode, 1, p.stdout + p.stderr)
        out = p.stdout + p.stderr
        self.assertIn("API key", out)
        self.assertIn(os.path.join(self.home, "keys.env"), out, "补救路径要落在这个 HOME 里")
        self.assertNotIn("Traceback", p.stderr)

    def test_entry_and_package_parse_under_python_3_7_grammar(self):
        # 闸门的前提：老解释器得先能把文件编译过，才轮得到闸门说话
        for path in [ENTRY] + sorted(glob.glob(os.path.join(ROOT, "core", "*.py"))):
            with open(path, encoding="utf-8") as f:
                ast.parse(f.read(), filename=path, feature_version=(3, 7))

    def test_version_gate_exits_2_and_names_the_minimum(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit) as cm:
            zylab._python_gate((3, 9, 7))
        self.assertEqual(cm.exception.code, 2)
        self.assertIn("3.10", err.getvalue())
        self.assertIn("3.9", err.getvalue())
        zylab._python_gate((3, 10, 0))   # 刚好达标不抛

    def test_preamble_order_is_gate_then_syspath_then_kc_import(self):
        with open(ENTRY, encoding="utf-8") as f:
            src = f.read()
        gate, syspath, core = (src.index("\n_python_gate()\n"),
                             src.index("sys.path.insert(0, os.path.dirname"),
                             src.index("\nfrom core import"))
        self.assertLess(gate, syspath)
        self.assertLess(syspath, core)


class _Tty(io.StringIO):
    def isatty(self):
        return True


class SilenceWatchTests(unittest.TestCase):
    def test_speaks_on_a_tty_after_silence_and_resets_on_events(self):
        out = _Tty()
        watch = zylab._SilenceWatch("g/m", stream=out)
        watch.FIRST, watch.EVERY = 0.3, 0.3
        watch.start()
        try:
            time.sleep(0.9)
            first = out.getvalue()
            self.assertIn("没有响应", first)
            watch.touch()
            time.sleep(0.2)
            self.assertEqual(out.getvalue(), first, "有事件后立刻闭嘴")
        finally:
            watch.stop()

    def test_stays_silent_when_stderr_is_a_pipe(self):
        out = io.StringIO()
        watch = zylab._SilenceWatch("g/m", stream=out)
        watch.FIRST = 0.1
        watch.start()
        time.sleep(0.4)
        watch.stop()
        self.assertEqual(out.getvalue(), "")
        self.assertIsNone(watch._thread)


if __name__ == "__main__":
    unittest.main()
