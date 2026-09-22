"""测试进程绝不能落到真实状态目录——而且这件事不能靠「每个测试记得自己隔离」。

2026-09-20 实查到的后果：实时 `models.json` 里有夹具造的 `test/m` 等三条、`usage.jsonl`
里 4 条 gateway="test"、记忆库里有 `echo test`；一次失败的测试运行还把一条假能力
（某 route「不许关思考」）持久化了，此后每次运行都受它影响。根因：基线命令
`python3 -m unittest discover -s tests` 把测试当顶层模块加载，不会 import
`tests/__init__.py` 里的隔离兜底。现在这道闸在 `core/__init__.py`。
"""
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

PROBE = textwrap.dedent('''
    import sys, unittest
    sys.path.insert(0, {root!r})

    class Probe(unittest.TestCase):
        def test_where_state_lands(self):
            from core import store          # 和绝大多数测试一样：不做任何隔离就 import
            print("STATE_HOME=" + str(store.HOME))
''')


class RunnerIsolationTests(unittest.TestCase):
    def run_probe(self, extra_env=None, argv=None):
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "test_probe_isolation.py"), "w", encoding="utf-8") as f:
                f.write(PROBE.format(root=ROOT))
            env = {k: v for k, v in os.environ.items()
                   if k not in ("ZYLAB_HOME", "ZYLAB_APP_ROOT")}
            env["HOME"] = tmp               # 真闸失效时也只会落到这个临时 HOME 里
            env.update(extra_env or {})
            proc = subprocess.run(
                argv or [sys.executable, "-m", "unittest", "test_probe_isolation"],
                cwd=tmp, env=env, capture_output=True, encoding="utf-8", errors="replace", text=True, timeout=120)
            out = proc.stdout + proc.stderr
            self.assertIn("STATE_HOME=", out, out[-600:])
            home = out.split("STATE_HOME=", 1)[1].splitlines()[0].strip()
            return tmp, home

    def test_a_test_that_forgets_to_isolate_still_lands_in_a_temp_dir(self):
        tmp, home = self.run_probe()
        self.assertIn("zylab-tests-", home)
        self.assertFalse(home.startswith(tmp), "落到了（假的）用户 HOME 里，闸没起作用")
        self.assertFalse(home.startswith(ROOT), "落到了应用目录的便携状态里")

    def test_an_explicit_home_is_respected(self):
        with tempfile.TemporaryDirectory() as chosen:
            _, home = self.run_probe({"ZYLAB_HOME": chosen})
            self.assertEqual(home, chosen)

    def test_running_a_test_file_directly_is_isolated_too(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = os.path.join(tmp, "test_direct_run.py")
            with open(script, "w", encoding="utf-8") as f:
                f.write(PROBE.format(root=ROOT) + "\nunittest.main()\n")
            _, home = self.run_probe(argv=[sys.executable, script])
            self.assertIn("zylab-tests-", home)

    def test_a_normal_process_is_left_alone(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {k: v for k, v in os.environ.items()
                   if k not in ("ZYLAB_HOME", "ZYLAB_APP_ROOT")}
            env["HOME"] = tmp
            env["ZYLAB_APP_ROOT"] = tmp     # 没有便携目录 → 默认 ~/.zylab
            code = ("import sys; sys.path.insert(0, %r); import core; from core import paths; "
                    "print(core.TEST_ISOLATION_ROOT, paths.state_home())" % ROOT)
            out = subprocess.run([sys.executable, "-c", code], env=env, cwd=tmp,
                                 capture_output=True, encoding="utf-8", errors="replace", text=True, timeout=60).stdout.strip()
            self.assertEqual(out, f"None {os.path.join(tmp, '.zylab')}")


if __name__ == "__main__":
    unittest.main()
