"""批次 1：core/paths.py 是全部路径与环境事实的单点。"""
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from core import paths

def _machine_path_roots():
    """这台机器专属的路径前缀 —— **推导出来，不写死**。

    闸门要守的规矩是「路径事实只能来自 paths.py 或配置」。以前这里硬编码了
    维护者机器的两个前缀，于是闸门自己成了仓库里最后一处机器标识。改成从
    已声明的受保护路径 + `$HOME` 推导：在维护者机器上等价于原来那两条，
    在别人的 clone 上自动变成他自己的，闸门的意义反而更强。
    """
    roots = [r for r in paths.protected_paths() if r not in ("/", "")]
    home = os.path.expanduser("~").rstrip("/")
    if home and home != "/":
        roots.append(home)
    return roots


EXACT_MACHINE_PATH = re.compile(
    r"""["'](?:%s)(?:/[^"']*)?/?["']"""
    % "|".join(re.escape(r) for r in _machine_path_roots() or ["\0"]))


def _docstring_lines(source):
    """模块 / 类 / 函数开头的 docstring 所占的行号——那是散文，不是代码。"""
    import ast
    tree = ast.parse(source)
    lines = set()
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if not isinstance(body, list) or not body:
            continue
        first = body[0]
        if (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)):
            lines.update(range(first.lineno, first.end_lineno + 1))
    return lines


def _code_lines(path):
    with open(path, encoding="utf-8") as f:
        source = f.read()
    skip = _docstring_lines(source)
    for n, line in enumerate(source.split("\n"), 1):
        if n in skip:
            continue
        yield n, line.split("#", 1)[0]


class PathsTests(unittest.TestCase):
    def test_derived_names_follow_app_name(self):
        with mock.patch.object(paths, "APP_NAME", "zylab"):
            self.assertEqual(paths.project_dirname(), ".zylab")
            self.assertEqual(paths.user_md_name(), "ZYLAB.md")
            self.assertEqual(paths.env_name("HOME"), "ZYLAB_HOME")
            with mock.patch.dict(os.environ, {}, clear=False), tempfile.TemporaryDirectory() as tmp:
                os.environ.pop("ZYLAB_HOME", None)
                os.environ["ZYLAB_APP_ROOT"] = tmp          # 没有便携目录 → 默认 ~/.zylab
                self.assertEqual(paths.state_home(), Path(os.path.expanduser("~")) / ".zylab")

    def test_state_home_override_must_be_exported_before_start(self):
        with mock.patch.dict(os.environ, {"ZYLAB_HOME": "/tmp/elsewhere"}):
            self.assertEqual(paths.state_home(), Path("/tmp/elsewhere"))

    def test_protected_paths_are_declared_not_hardcoded(self):
        """默认一条都没有 —— 不预设任何一台机器的挂载布局。

        早先这里断言必须含某个具体挂载点，前提是「同事的机器长得一样」。
        那个前提是错的：同一个集群的机器挂载也不同。
        """
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("ZYLAB_PROTECTED_PATHS", None)
            os.environ.pop("ZYLAB_PROTECTED_REMOTES", None)
            self.assertEqual(paths.protected_paths(), [])
            self.assertEqual(paths.protected_remotes(), [])
            self.assertFalse(paths.is_protected("/anywhere"))

    def test_a_declaration_behind_a_symlink_is_still_guarded(self):
        """**声明经过软链时，两种形态都要守。**

        比较本身（`paths.path_under`）是纯字符串比较，而调用点各有各的形态：
        `tools._guard` 比的是 realpath 过的候选，`_guard_bash` 的词法守卫比的是
        命令原文（它没法解析任意 shell）。原来 `protected_paths()` 只留声明形态，
        于是前一类在「声明经过软链」时**恒不成立**——2026-09-22 实测：声明一条
        指向真实目录的软链之后，经软链写和经真实路径写**都被放行**，
        而界面上看不出任何异常。

        这是 `paths.path_under` 当初被建出来的那类事故的另一道门（那次是
        Windows 上 `startswith(root + "/")` 恒为 False）。macOS 上尤其容易碰到：
        `/tmp`→`/private/tmp`、`/var`→`/private/var` 都是系统软链。
        """
        with tempfile.TemporaryDirectory() as tmp:
            real = Path(tmp) / "real"
            real.mkdir()
            link = Path(tmp) / "link"
            try:
                link.symlink_to(real, target_is_directory=True)
            except (OSError, NotImplementedError):
                self.skipTest("本机不能创建符号链接（Windows 需管理员/开发者模式）")
            with mock.patch.dict(
                    os.environ, {"ZYLAB_PROTECTED_PATHS": str(link)}):
                roots = paths.protected_paths()
                # 两种形态都在
                self.assertIn(str(link), roots)
                self.assertIn(os.path.realpath(link), roots)
                # 两条路都拦得住
                self.assertTrue(paths.is_protected(str(link / "x")))
                self.assertTrue(paths.is_protected(str(real / "x")))
                # 守多了不行：不相干的兄弟目录不该被误拦
                self.assertFalse(paths.is_protected(str(Path(tmp) / "other")))

    def test_a_plain_declaration_stays_a_single_entry(self):
        """realpath 恒等时不该冒出重复项——「守多了」也要有边界。"""
        with mock.patch.dict(
                os.environ, {"ZYLAB_PROTECTED_PATHS": "/no-such-archive"}):
            self.assertEqual(paths.protected_paths(), ["/no-such-archive"])

    def test_declared_protected_paths_take_effect(self):
        with mock.patch.dict(os.environ, {
                "ZYLAB_PROTECTED_PATHS": os.pathsep.join(
                    ["/data/archive", "/mnt/readonly/"]),
                "ZYLAB_PROTECTED_REMOTES": "arch:bucket,other:x"}):
            self.assertEqual(
                paths.protected_paths(), ["/data/archive", "/mnt/readonly"])
            self.assertEqual(
                paths.protected_remotes(), ["arch:bucket", "other:x"])
            self.assertTrue(paths.is_protected("/data/archive"))
            self.assertTrue(paths.is_protected("/data/archive/x"))
            self.assertFalse(paths.is_protected("/data/archivex"))

    def test_settings_declared_paths_reach_the_lexical_guard(self):
        """回归闸门：配置里声明的受保护路径必须同时进**词法守卫**。

        沙箱只读挂载和 core/tools.py 的 `PROTECTED` 是两条独立的路。曾经
        `protected_paths()` 只读环境变量，于是在 settings 里声明只有挂载那半边
        生效，`_guard_bash` 一律放行 —— 配了但半边不管用，**界面上看不出来**。
        `PROTECTED` 是 import 时求值的，所以只能开子进程验，不能在本进程 patch。
        """
        import json
        import subprocess
        with tempfile.TemporaryDirectory() as tmp:
            with open(os.path.join(tmp, "settings.json"), "w", encoding="utf-8") as fh:
                json.dump({"sandbox": {"protected_paths": ["/declared/archive"],
                                       "protected_remotes": ["arch:bucket"]}}, fh)
            env = dict(os.environ, ZYLAB_HOME=tmp)
            env.pop("ZYLAB_PROTECTED_PATHS", None)
            env.pop("ZYLAB_PROTECTED_REMOTES", None)
            out = subprocess.run(
                [sys.executable, "-c",
                 "import sys; sys.path.insert(0, %r)\n"
                 "from core import paths, tools\n"
                 "print(paths.protected_paths())\n"
                 "print(tools.PROTECTED)\n"
                 "print(tools.PROTECTED_REMOTES)\n"
                 "print(tools._under_protected('/declared/archive/x'))" % ROOT],
                capture_output=True, encoding="utf-8", errors="replace", text=True, env=env, cwd=ROOT, timeout=120)
            self.assertEqual(out.returncode, 0, out.stderr)
            declared, protected, remotes, under = out.stdout.strip().split("\n")
            self.assertIn("/declared/archive", declared)
            self.assertIn("/declared/archive", protected)
            self.assertIn("arch:bucket", remotes)
            self.assertEqual(under, "True")

    def test_machine_facts_has_exactly_two_keys(self):
        self.assertEqual(set(paths.machine_facts()), {"python_version", "has_rg"})

    def test_legacy_files_only_when_present(self):
        with mock.patch("os.path.exists", return_value=False):
            self.assertEqual(paths.legacy_key_files(), ())
            self.assertEqual(paths.legacy_env_files(), ())

    def test_no_exact_machine_path_literal_outside_paths_py(self):
        """闸门：代码里不得出现任何一台具体机器的路径字面量。

        以前这条允许 paths.py 例外——那时受保护路径是写死的常量，总得有个
        地方写。改成声明式之后这个例外就没有理由了，所以 paths.py 一并纳入。
        散文（docstring / 注释）不算：那里举例说明是合理的。"""
        offenders = []
        files = [os.path.join(ROOT, "zylab.py")] + sorted(
            os.path.join(ROOT, "core", f) for f in os.listdir(os.path.join(ROOT, "core"))
            if f.endswith(".py"))
        for path in files:
            for n, code in _code_lines(path):
                if EXACT_MACHINE_PATH.search(code):
                    offenders.append(f"{os.path.relpath(path, ROOT)}:{n}")
        self.assertEqual(offenders, [], "\n".join(offenders))

    def test_no_tilde_state_dir_literal_outside_paths_py(self):
        offenders = []
        files = [os.path.join(ROOT, "zylab.py")] + sorted(
            os.path.join(ROOT, "core", f) for f in os.listdir(os.path.join(ROOT, "core"))
            if f.endswith(".py"))
        for path in files:
            for n, code in _code_lines(path):
                if 'expanduser("~/' in code or "~/.zylab" in code:
                    offenders.append(f"{os.path.relpath(path, ROOT)}:{n}")
        self.assertEqual(offenders, [], "\n".join(offenders))


if __name__ == "__main__":
    unittest.main()
