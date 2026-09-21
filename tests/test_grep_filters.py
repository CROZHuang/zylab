"""grep 工具的过滤与独立性契约（2026-09-18）。

**这个工具现在是进程内实现，不依赖系统上的 grep。** 之所以走到这一步，是因为
外包给 `grep` 踩了两个都**沉默失效**的坑 —— 不报错，只是悄悄给错结果：

1. **`--include` 必须排在 `--exclude` 之前。** GNU grep 把两者放进同一张有序表、
   按「先匹配者胜」处理。排在一堆 `--exclude=*.png` 之后的 `--include=*.py`，
   对既不是 .png 也不是 .py 的文件一条都匹配不上，于是走「默认收下」——
   `glob` 参数形同虚设，搜 `*.py` 会把 .txt 一起交出来。与平台无关（实测 grep 3.0）。

2. **带通配符的选项在 Windows 上会被拆掉。** 那里的 grep 是 Git for Windows 的
   MSYS 程序，MSYS 运行时会对自己的 argv 再做一次 glob 展开：
   `["--include", "*.py"]` 变成 `--include a.py c.py`，过滤器只剩第一个文件，
   其余文件名还成了**额外的搜索路径**；`["-e", "*.py"]` 更糟，搜的直接不是
   用户要的东西。

这两条都不是「修好某一版就完了」，而是**依赖外部工具**本身带来的，所以改成
自己走树。本文件因此钉两类东西：**过滤语义**（glob/排除项确实生效），
以及**独立性**（不起子进程、系统没有 grep 也照常工作）。
"""
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = str(Path(__file__).resolve().parents[1])
sys.path.insert(0, ROOT)

from core import tools  # noqa: E402


class GrepBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        self._cwd = os.getcwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, self._cwd)
        for name, body in (("a.py", b"NEEDLE py\n"),
                           ("b.txt", b"NEEDLE txt\n"),
                           ("c.py", b"NEEDLE py again\n"),
                           ("x.png", b"NEEDLE png\n")):
            (self.root / name).write_bytes(body)
        (self.root / "sub").mkdir()
        (self.root / "sub" / "deep.py").write_bytes(b"NEEDLE deep\n")

    def names(self, output):
        """从工具输出里取出命中的文件名（路径可能含 Windows 盘符冒号）。"""
        found = set()
        for line in output.splitlines():
            match = re.match(r"^(.*?):(\d+):", line)
            if match:
                found.add(os.path.basename(match.group(1)))
        return sorted(found)


class GrepFilters(GrepBase):
    def test_glob_actually_restricts_the_file_set(self):
        self.assertEqual(
            self.names(tools.t_grep("NEEDLE", ".", glob="*.py")),
            ["a.py", "c.py", "deep.py"])

    def test_glob_excludes_other_extensions(self):
        self.assertEqual(
            self.names(tools.t_grep("NEEDLE", ".", glob="*.txt")), ["b.txt"])

    def test_binary_extensions_stay_excluded(self):
        self.assertNotIn("x.png", self.names(tools.t_grep("NEEDLE", ".")))

    def test_star_glob_still_excludes_binary_extensions(self):
        self.assertNotIn(
            "x.png", self.names(tools.t_grep("NEEDLE", ".", glob="*")))

    def test_cache_directories_are_skipped_by_basename(self):
        junk = self.root / "node_modules"
        junk.mkdir()
        (junk / "dep.py").write_bytes(b"NEEDLE vendored\n")
        self.assertNotIn("dep.py", self.names(tools.t_grep("NEEDLE", ".")))

    def test_files_containing_nul_are_treated_as_binary(self):
        (self.root / "blob.dat").write_bytes(b"NEEDLE\x00after\n")
        self.assertNotIn("blob.dat", self.names(tools.t_grep("NEEDLE", ".")))

    def test_line_numbers_and_content_are_reported(self):
        (self.root / "multi.py").write_bytes(b"one\nNEEDLE here\n")
        hit = [l for l in tools.t_grep("NEEDLE here", ".").splitlines()
               if "multi.py" in l]
        self.assertTrue(hit, "应命中 multi.py")
        self.assertRegex(hit[0], r":2:NEEDLE here$")


class GrepNeedsNoSystemTools(GrepBase):
    """独立性：这是换掉外部 grep 的全部理由，必须钉死。"""

    def test_no_subprocess_is_spawned(self):
        started = []
        real_popen, real_run = tools.subprocess.Popen, tools.subprocess.run

        def spy_popen(cmd, *a, **k):
            started.append(cmd)
            return real_popen(cmd, *a, **k)

        def spy_run(cmd, *a, **k):
            started.append(cmd)
            return real_run(cmd, *a, **k)

        tools.subprocess.Popen, tools.subprocess.run = spy_popen, spy_run
        try:
            output = tools.t_grep("NEEDLE", ".", glob="*.py")
        finally:
            tools.subprocess.Popen, tools.subprocess.run = real_popen, real_run
        self.assertEqual(started, [], "grep 不该启动任何子进程")
        self.assertIn("a.py", self.names(output))

    def test_still_works_when_spawning_is_impossible(self):
        """机器上没有 grep（或根本不让起进程）时，搜索照常。"""
        real_popen, real_run = tools.subprocess.Popen, tools.subprocess.run

        def boom(*_a, **_k):
            raise FileNotFoundError("[WinError 2] 找不到指定的文件。")

        tools.subprocess.Popen = tools.subprocess.run = boom
        try:
            output = tools.t_grep("NEEDLE", ".")
        finally:
            tools.subprocess.Popen, tools.subprocess.run = real_popen, real_run
        self.assertEqual(self.names(output),
                         ["a.py", "b.txt", "c.py", "deep.py"])


class GrepRegexDialect(GrepBase):
    """刻意的语义变化：正则方言是 Python `re`，不是 grep 的 POSIX BRE。"""

    def test_pcre_style_patterns_work_unescaped(self):
        (self.root / "d.py").write_bytes(b"value = 42\n")
        self.assertIn("d.py", self.names(tools.t_grep(r"\d+", ".")))
        self.assertIn("a.py", self.names(tools.t_grep(r"(NEEDLE|MISSING)", ".")))

    def test_invalid_regex_reports_instead_of_raising(self):
        output = tools.t_grep("(unclosed", ".")
        self.assertIn("正则无效", output)

    def test_ignore_case(self):
        self.assertIn(
            "a.py", self.names(tools.t_grep("needle", ".", ignore_case=True)))
        self.assertEqual(self.names(tools.t_grep("needle", ".")), [])


if __name__ == "__main__":
    unittest.main()
