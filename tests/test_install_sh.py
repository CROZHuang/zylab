"""§2.5 install.sh：薄 stub（普通文件，非 symlink），幂等，可卸载；从别的目录跑 stub 能起入口。

同时钉住 Windows 那一侧的入口 `zylab.cmd` —— cmd / PowerShell 不认 shebang，
没有它的话 Windows 用户克隆下来第一条命令就跑不起来。"""
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


class WindowsLauncherTests(unittest.TestCase):
    """`zylab.cmd`：Windows 上 `./zylab` 的对应物。

    内容契约在两个平台上都断言 —— 这个文件最容易在重命名或"清理根目录"时
    被顺手删掉，而 Linux 上没有任何东西会因此变红。
    """

    def setUp(self):
        self.path = os.path.join(ROOT, "zylab.cmd")
        self.assertTrue(os.path.isfile(self.path), "Windows 入口不见了")
        with open(self.path, encoding="utf-8") as f:
            self.body = f.read()

    def test_it_only_forwards_and_never_reimplements_anything(self):
        self.assertIn("zylab.py", self.body)
        for leak in ("import core", "settings.json", "keys.env", "/v1/"):
            self.assertNotIn(leak, self.body, f"启动器里不该有 {leak}")

    def test_it_tries_both_ways_windows_actually_spells_python(self):
        """`python3` 在 Windows 上基本不存在；没装 Python 时 python.exe 是商店别名。"""
        self.assertIn("py -3", self.body)
        self.assertIn("python -c", self.body)
        self.assertEqual(self.body.count("version_info >= (3, 10)"), 2,
                         "两条候选路径都要先验版本，不能直接跑")

    def test_a_missing_interpreter_says_what_to_do(self):
        for hint in ("python.org", "git-scm.com", "应用执行别名"):
            self.assertIn(hint, self.body)

    @unittest.skipUnless(sys.platform == "win32", "要 cmd.exe 才跑得起来")
    def test_it_actually_starts_the_entry(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(os.environ,
                       ZYLAB_HOME=os.path.join(tmp, "home"),
                       ZYLAB_KEYS_FILE=os.path.join(tmp, "keys.env"),
                       PYTHONIOENCODING="utf-8")
            run = subprocess.run([self.path, "--help"], cwd=tmp, env=env,
                                 capture_output=True, text=True, timeout=120)
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertIn("zylab", run.stdout)


class EntryPointsStayExecutable(unittest.TestCase):
    """`./zylab` 与 `./install.sh` 在**git 记录的模式**里必须是 100755。

    2026-09-17 的 Windows 快照提交（`c4046a9`）把两者从 100755 改成了 100644 ——
    Windows 上 git 不记录可执行位，一次往返就把它抹掉了。于是 README 快速开始的
    第一条命令 `./zylab init` 对**每一个下载者**都是 `Permission denied`，而在
    维护者自己的机器上（文件早就 chmod 过）完全看不出来。公开仓库带着这个问题
    发布了至少一轮。

    查 git 的模式而不是文件系统的位：后者是每份检出各自的状态，前者才是别人
    clone 下来拿到的东西。
    """

    ENTRIES = ("zylab", "install.sh")
    # 同一次 Windows 往返把这些也一起抹平了。它们平时是 `python3 scripts/x.py`
    # 调的，坏了不至于挡住用户，但「有 shebang 的文件一半可执行一半不可」本身
    # 就是下一次漏判的温床 —— 整份钉住，多了少了都算回归。
    EXECUTABLE = ENTRIES + (
        "scripts/bench_session_picker.py", "scripts/bench_tui_latency.py",
        "scripts/build_model_seed.py", "scripts/cc_parity.py",
        "scripts/harvest_specs.py", "scripts/make_tarball.sh",
        "scripts/manage_graft.py", "scripts/run_skill_eval.py",
        "scripts/smoke_slash_commands.py", "scripts/term_probe.py",
        "zylab.py")

    def modes(self):
        probe = subprocess.run(
            ["git", "ls-files", "-s"],
            cwd=ROOT, capture_output=True, text=True, timeout=30)
        if probe.returncode != 0:
            self.skipTest(f"这里跑不了 git：{probe.stderr.strip()[:80]}")
        out = {}
        for line in probe.stdout.splitlines():
            mode, _, rest = line.partition(" ")
            out[rest.split("\t")[-1]] = mode
        self.assertTrue(out, "git 一个条目都没列出来")
        return out

    def test_the_two_entry_points_are_executable(self):
        """README 快速开始的第一条命令就是 `./zylab init`。"""
        modes = self.modes()
        for name in self.ENTRIES:
            with self.subTest(entry=name):
                self.assertEqual(
                    modes.get(name), "100755",
                    f"{name} 在 git 里是 {modes.get(name)}——"
                    "新克隆下来的人执行它会 Permission denied")

    def test_the_executable_set_is_exactly_what_it_should_be(self):
        modes = self.modes()
        actual = {name for name, mode in modes.items() if mode == "100755"}
        self.assertEqual(
            actual, set(self.EXECUTABLE),
            "可执行文件的集合变了：多出来的要么是误加，"
            "少掉的多半又是一次跨平台往返抹掉的")


if __name__ == "__main__":
    unittest.main()
