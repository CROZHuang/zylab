"""README 承诺 Python 3.10+，那就不能写只有 3.12 才认的语法。

2026-09-21 的真事故：公开仓库的第一次 CI，`ubuntu-latest · py3.10` 那一列
**八个测试模块 import 不了**，报 `unterminated string literal`。根因是三处
PEP 701 语法——f-string 的**替换字段**里换行、或带反斜杠。这两样在 3.12 才放开，
在 3.10 / 3.11 上是 `SyntaxError`，也就是说**照着 README 用 3.10 的人，
`import core.tui` 就崩**。维护者机器上只有 3.12，本地全量一直是绿的。

这条用例用 3.12+ 自带的 tokenizer 直接查形态，不需要另装一个旧解释器——
否则它就只能在 CI 上生效，而 CI 正是发现得最晚的地方。
（`ast.parse(feature_version=(3, 10))` 查不出来：feature_version 不影响
词法分析，实测放行。）
"""
import io
import subprocess
import sys
import tokenize
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import tests  # noqa: F401,E402  —— 状态目录隔离

# README 里写的那个数。改这里之前先改 README，并想清楚要不要把用户挡在门外。
FLOOR = (3, 10)


def pep701_offences(path):
    """返回 [(行号, 原因)]：替换字段里换行、或替换字段里有反斜杠。

    只看**替换字段**内部。三引号 f-string 的字面量部分跨多行是合法的
    （`tests/test_migrations.py` 里就有一处），不能一起算进来。
    """
    source = Path(path).read_text(encoding="utf-8")
    out, fstring, braces, opened_at = [], 0, 0, 0
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        kind = tokenize.tok_name[token.type]
        if kind == "FSTRING_START":
            fstring += 1
            continue
        if kind == "FSTRING_END":
            fstring = max(0, fstring - 1)
            braces = 0
            continue
        if not fstring:
            continue
        if kind == "OP" and token.string == "{":
            braces += 1
            opened_at = token.start[0]
            continue
        if kind == "OP" and token.string == "}":
            braces = max(0, braces - 1)
            continue
        if not braces:
            continue
        if kind in ("NL", "NEWLINE"):
            out.append((opened_at, "替换字段里换行"))
        elif "\\" in token.string:
            out.append((token.start[0], "替换字段里有反斜杠"))
    return out


@unittest.skipIf(sys.version_info < (3, 12),
                 "形态探针依赖 3.12 的 FSTRING_* token；在更低的解释器上，"
                 "下面那条「真编译一遍」的用例本身就是更强的检查")
class OnlySyntaxTheFloorUnderstands(unittest.TestCase):
    def tracked(self):
        done = subprocess.run(["git", "ls-files", "*.py"], cwd=ROOT,
                              capture_output=True, encoding="utf-8", errors="replace", text=True, timeout=30)
        if done.returncode != 0:
            self.skipTest(f"这里跑不了 git：{done.stderr.strip()[:80]}")
        files = [ROOT / name for name in done.stdout.split()]
        self.assertGreater(len(files), 100, "git 只列出了这么几个文件？")
        return files

    def test_no_pep701_only_f_strings(self):
        offences = []
        for path in self.tracked():
            for line, why in pep701_offences(path):
                offences.append(f"{path.relative_to(ROOT)}:{line} {why}")
        self.assertEqual(
            offences, [],
            "这些写法要 Python 3.12（PEP 701），而 README 承诺 "
            f"{FLOOR[0]}.{FLOOR[1]}+：\n  " + "\n  ".join(offences)
            + "\n先把值算出来再插值即可。")

    def test_the_detector_actually_detects(self):
        """探针自己得先证明它抓得住——否则这条用例只是个恒绿的摆设。"""
        newline_case = 'x = f"{foo(\n    1)}"\n'
        backslash_case = 'x = f"{bar(\'a\\"b\')}"\n'
        for source, expect in ((newline_case, "换行"), (backslash_case, "反斜杠")):
            with self.subTest(expect=expect):
                path = Path(self.tmp) / "probe.py"
                path.write_text(source, encoding="utf-8")
                found = pep701_offences(path)
                self.assertTrue(found, f"没抓到{expect}：{source!r}")
                self.assertIn(expect, found[0][1])

    def test_a_multiline_literal_is_not_an_offence(self):
        """三引号 f-string 的字面量跨行是合法的，不许误报。"""
        path = Path(self.tmp) / "ok.py"
        path.write_text('x = f"""\nhello {name}\nbye\n"""\n', encoding="utf-8")
        self.assertEqual(pep701_offences(path), [])

    def setUp(self):
        import tempfile
        holder = tempfile.TemporaryDirectory(prefix="zylab-floor-")
        self.addCleanup(holder.cleanup)
        self.tmp = holder.name


def floor_interpreter():
    """PATH 上有没有最低版本的解释器。没有就返回 None。

    刻意只问 PATH（`shutil.which`），不写任何一台机器的绝对路径——这个文件是
    随仓库公开发布的。
    """
    import shutil
    return shutil.which(f"python{FLOOR[0]}.{FLOOR[1]}")


@unittest.skipUnless(
    floor_interpreter(),
    f"PATH 上没有 python{FLOOR[0]}.{FLOOR[1]}；形态检查在上面那条用例里，"
    "CI 的最低版本那一列是兜底")
class TheFloorInterpreterAgrees(unittest.TestCase):
    """有最低版本的解释器就真编译一遍——形态探针再好也只是形态。"""

    def test_every_tracked_file_compiles_on_the_floor(self):
        exe = floor_interpreter()
        probe = (
            "import subprocess, pathlib, sys\n"
            "files = subprocess.run(['git','ls-files','*.py'],"
            " capture_output=True, text=True).stdout.split()\n"
            "bad = []\n"
            "for f in files:\n"
            "    try: compile(pathlib.Path(f).read_text(encoding='utf-8'), f, 'exec')\n"
            "    except SyntaxError as e: bad.append(f'{f}:{e.lineno} {e.msg}')\n"
            "print('\\n'.join(bad))\n")
        done = subprocess.run([exe, "-c", probe], cwd=ROOT,
                              capture_output=True, encoding="utf-8", errors="replace", text=True, timeout=180)
        self.assertEqual(done.returncode, 0, done.stderr[-400:])
        self.assertEqual(done.stdout.strip(), "", done.stdout)


class AZeroSizedTerminalIsUnknownNotZero(unittest.TestCase):
    """`shutil.get_terminal_size()` 在 3.11 之前不把 0 当成「查不到」。

    没设过 `TIOCSWINSZ` 的 pty 上它原样返回 `(0, 0)`（3.11 起才回落到
    `(80, 24)`）。`core.tui._cols` 原来写 `max(20, columns)`，于是整个界面被压成
    **20 列**，每一行都截成「…」。2026-09-21 由公开仓库 CI 的 py3.10 那一列抓到。

    这不是只有旧解释器才会遇到的事：任何一个查不到尺寸的终端都一样。
    """

    def sizes(self, columns, rows):
        import os
        import shutil
        from unittest import mock
        from core import tui
        with mock.patch.object(shutil, "get_terminal_size",
                               return_value=os.terminal_size((columns, rows))):
            return tui._cols(), tui._lines()

    def test_zero_falls_back_to_something_usable(self):
        cols, rows = self.sizes(0, 0)
        self.assertGreaterEqual(cols, 80, "20 列的界面等于没有界面")
        self.assertGreaterEqual(rows, 24)

    def test_a_real_size_is_honoured(self):
        self.assertEqual(self.sizes(132, 50), (132, 50))

    def test_a_silly_but_nonzero_size_still_gets_a_floor(self):
        cols, rows = self.sizes(3, 1)
        self.assertEqual((cols, rows), (20, 5))


if __name__ == "__main__":
    unittest.main()
