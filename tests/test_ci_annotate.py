"""CI 的失败报告器本身要有测试——它坏了的代价是「CI 红了但不知道为什么」。

2026-09-21 实际发生过：第一版把报告逻辑写成 workflow 里的 heredoc，Windows 上
一条注解都没出来（Git Bash 的 `tee /tmp/x` 与 Windows python 的 `open("/tmp/x")`
不是同一个地方）。那两轮 CI 的失败信息因此全部丢掉，只剩
`Process completed with exit code 1`。
"""
import io
import subprocess
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))

import tests  # noqa: F401,E402  —— 状态目录隔离

import ci_annotate  # noqa: E402

UNITTEST_LOG = """\
..F.E.
======================================================================
FAIL: test_alpha (tests.test_thing.Cases.test_alpha)
----------------------------------------------------------------------
Traceback (most recent call last):
  File "tests/test_thing.py", line 10, in test_alpha
    self.assertEqual(1, 2)
AssertionError: 1 != 2

======================================================================
ERROR: test_beta (tests.test_thing.Cases.test_beta)
----------------------------------------------------------------------
Traceback (most recent call last):
  File "tests/test_thing.py", line 14, in test_beta
    raise RuntimeError("boom 50%")
RuntimeError: boom 50%

----------------------------------------------------------------------
Ran 6 tests in 1.234s

FAILED (failures=1, errors=1)
"""


class Packing(unittest.TestCase):
    """注解每步只显示 10 条，所以必须打包，不能一行一个失败名。"""

    def titles(self, log):
        return [title for title, _ in ci_annotate.annotations(log)]

    def test_a_failing_log_gives_list_tracebacks_and_tail(self):
        got = ci_annotate.annotations(UNITTEST_LOG)
        self.assertEqual([t for t, _ in got],
                         ["根因汇总", "失败清单", "traceback 1", "traceback 2",
                          "尾部"])
        bodies = dict(got)
        self.assertIn("test_alpha", bodies["失败清单"])
        self.assertIn("test_beta", bodies["失败清单"])
        self.assertIn("AssertionError: 1 != 2", bodies["traceback 1"])
        self.assertIn("RuntimeError", bodies["traceback 2"])
        self.assertIn("FAILED (failures=1, errors=1)", bodies["尾部"])

    def test_it_never_emits_more_than_ten(self):
        """再多的失败也只能打包成几条——超过 10 条 GitHub 直接不显示。"""
        many = "".join(
            UNITTEST_LOG.replace("test_alpha", f"test_{i:03d}")
            for i in range(30))
        self.assertLessEqual(len(ci_annotate.annotations(many)), 10)

    def test_a_parity_regression_is_reported(self):
        log = "  ❌ F1  粘贴  没有进入括号粘贴\n  ⁉ K2  recap  找不到\nparity = 47/49\n"
        bodies = dict(ci_annotate.annotations(log))
        self.assertIn("记分板回归", bodies)
        self.assertIn("F1", bodies["记分板回归"])

    def test_a_green_log_says_nothing_alarming(self):
        green = "..........\n" + "-" * 70 + "\nRan 10 tests in 0.1s\n\nOK\n"
        self.assertEqual(self.titles(green), ["尾部"])


class RootCauses(unittest.TestCase):
    """40 条 ERROR 是 1 个根因还是 40 个 —— 不归并就看不出来。

    2026-09-22 公开仓库 CI 的 Windows py3.10 那列：失败清单 40 条，两条
    traceback **是同一个** `chmod: follow_symlinks unavailable`。
    40 条失败只换到 1 份信息，而且当时无法判断另外 38 条是不是同一件事。
    """

    def block(self, name, exc, *, tmp="tmpabc123"):
        return (f"{'=' * 70}\n"
                f"ERROR: {name} (tests.test_thing.Cases.{name})\n"
                f"{'-' * 70}\n"
                "Traceback (most recent call last):\n"
                f'  File "tests/test_thing.py", line 9, in {name}\n'
                f"    store.open(r'C:\\Users\\RUNNER~1\\AppData\\Local\\Temp\\{tmp}\\x')\n"
                f"{exc}\n")

    def log(self, *blocks):
        return ("..EEE\n" + "\n".join(blocks) + "\n" + "-" * 70
                + "\nRan 5 tests in 1s\n\nFAILED (errors=3)\n")

    def test_one_shared_cause_is_reported_once_with_a_count(self):
        same = "NotImplementedError: chmod: follow_symlinks unavailable"
        log = self.log(*[self.block(f"test_{i}", same, tmp=f"tmp{i}zz")
                         for i in range(12)])
        bodies = dict(ci_annotate.annotations(log))
        self.assertIn("1 类根因 / 12 个失败块", bodies["根因汇总"])
        self.assertIn("12×", bodies["根因汇总"])
        self.assertIn("首例 ERROR: test_0", bodies["根因汇总"])

    def test_each_cause_gets_at_most_one_traceback(self):
        """选 traceback 按**根因**去重，否则两条注解讲同一件事。"""
        log = self.log(
            self.block("test_a", "NotImplementedError: chmod: nofollow"),
            self.block("test_b", "NotImplementedError: chmod: nofollow"),
            self.block("test_c", "PermissionError: [WinError 5] Access is denied"))
        got = dict(ci_annotate.annotations(log))
        self.assertIn("2 类根因 / 3 个失败块", got["根因汇总"])
        self.assertIn("NotImplementedError", got["traceback 1"])
        self.assertIn("WinError 5", got["traceback 2"])
        self.assertNotIn("traceback 3", got)

    def test_the_key_ignores_paths_and_numbers_but_not_the_exception(self):
        self.assertEqual(
            ci_annotate.cause_key(
                r"PermissionError: [WinError 5] denied: 'C:\Temp\tmp12ab\x'"),
            ci_annotate.cause_key(
                r"PermissionError: [WinError 5] denied: 'C:\Temp\tmp99zz\y'"),
            "同一个根因在不同临时目录上不该算两类")
        self.assertNotEqual(
            ci_annotate.cause_key("PermissionError: [WinError 5] denied"),
            ci_annotate.cause_key("PermissionError: [WinError 32] in use"),
            "winerror 不同就是不同的根因，不能一起抹掉")

    def test_the_cause_is_the_last_exception_not_the_first(self):
        """链式异常（`During handling of…`）里，最后那个才是落地的根因。"""
        chained = (f"{'=' * 70}\n"
                   "ERROR: test_x (tests.test_thing.Cases.test_x)\n"
                   f"{'-' * 70}\n"
                   "Traceback (most recent call last):\n"
                   "  File \"a.py\", line 1, in test_x\n"
                   "PermissionError: [WinError 5] denied\n"
                   "\nDuring handling of the above exception, "
                   "another exception occurred:\n\n"
                   "Traceback (most recent call last):\n"
                   "  File \"b.py\", line 2, in test_x\n"
                   "ManifestError: 无法收紧 checkpoint 目录权限\n")
        bodies = dict(ci_annotate.annotations(self.log(chained)))
        self.assertIn("ManifestError", bodies["根因汇总"])

    def test_a_green_log_has_no_cause_annotation(self):
        green = "....\n" + "-" * 70 + "\nRan 4 tests in 0.1s\n\nOK\n"
        self.assertNotIn("根因汇总", dict(ci_annotate.annotations(green)))


class Escaping(unittest.TestCase):
    """GitHub 的 workflow command 是单行的：换行和 % 不转义就会被截掉。"""

    def test_newlines_and_percent_are_escaped(self):
        body = ci_annotate.escape("一行\n二行 50% 完成\r\n三行")
        self.assertNotIn("\n", body)
        self.assertNotIn("\r", body)
        self.assertIn("%0A", body)
        self.assertIn("50%25", body)

    def test_a_huge_body_is_capped(self):
        self.assertLessEqual(len(ci_annotate.escape("x" * 99_999)),
                             ci_annotate.MAX_BODY)


class AsACommand(unittest.TestCase):
    """workflow 直接 `python scripts/ci_annotate.py <日志>` 调它。"""

    def setUp(self):
        import tempfile
        holder = tempfile.TemporaryDirectory(prefix="zylab-ann-")
        self.addCleanup(holder.cleanup)
        self.tmp = Path(holder.name)

    def run_it(self, *args):
        # 显式 PIPE 而不是 capture_output=True：两者等价，但少一个「被哪一层
        # 吞掉了关键字」的可能（见 shape() 里那次 Windows 上的 stdout=None）。
        return subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "ci_annotate.py"), *args],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            encoding="utf-8", errors="replace", text=True, timeout=60)

    def shape(self, done):
        """把 CompletedProcess 的形状写进断言消息里。

        2026-09-22 Windows CI 上这两条用例是
        `AttributeError: 'NoneType' object has no attribute 'splitlines'`——
        `capture_output=True` 却拿到 `stdout is None`，而 AttributeError 的栈里
        看不出 CompletedProcess 到底长什么样，只能靠猜。**猜不动就让它自己说。**
        """
        return (f"returncode={done.returncode!r} "
                f"stdout={type(done.stdout).__name__} {done.stdout!r:.300} "
                f"stderr={type(done.stderr).__name__} {done.stderr!r:.300} "
                f"run={subprocess.run!r:.200}")

    def test_it_prints_error_commands(self):
        log = self.tmp / "unit.log"
        log.write_text(UNITTEST_LOG, encoding="utf-8")
        done = self.run_it(str(log))
        self.assertEqual(done.returncode, 0, self.shape(done))
        self.assertIsNotNone(done.stdout, self.shape(done))
        lines = [l for l in done.stdout.splitlines() if l.startswith("::error")]
        self.assertGreaterEqual(len(lines), 3)
        for line in lines:
            self.assertNotIn("\t", line)

    def test_a_missing_log_warns_but_never_masks_the_real_failure(self):
        """报告器自己坏掉，不该把被报告的那个失败盖住——所以退出码是 0。"""
        done = self.run_it(str(self.tmp / "nope.log"))
        self.assertEqual(done.returncode, 0, self.shape(done))
        self.assertIsNotNone(done.stdout, self.shape(done))
        self.assertIn("::warning::", done.stdout)

    def test_wrong_usage_is_a_usage_error(self):
        self.assertEqual(self.run_it().returncode, 2)


class ItSurvivesALegacyCodePage(unittest.TestCase):
    """Windows 上写管道时 Python 用本地代码页，cp1252/cp936 编不出中文标题。

    2026-09-21 **连续两轮** CI 就栽在这里：报告器 `UnicodeEncodeError` 当场死掉，
    workflow 里那句 `|| true` 把它咽掉，于是「红了但一条注解都没有」。
    zylab 自己有 `wincompat.configure_stdio()` 干同一件事——同一个坑我在 CI 上
    又踩了一遍。
    """

    def cp1252_stream(self):
        return io.TextIOWrapper(io.BytesIO(), encoding="cp1252", errors="strict")

    def test_a_legacy_code_page_stream_gets_reconfigured(self):
        stream = self.cp1252_stream()
        self.assertEqual(stream.encoding, "cp1252")
        ci_annotate.force_utf8_stdout(stream)
        self.assertEqual(stream.encoding, "utf-8")
        stream.write("失败清单\n")                 # 不崩就是通过

    def test_chinese_titles_still_get_printed_on_a_legacy_code_page(self):
        """端到端：整条 main() 在 cp1252 的 stdout 上也要把注解打出来。"""
        import tempfile
        from unittest import mock
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "unit.log"
            log.write_text(UNITTEST_LOG, encoding="utf-8")
            stream = self.cp1252_stream()
            with mock.patch.object(ci_annotate.sys, "stdout", stream):
                rc = ci_annotate.main(["ci_annotate.py", str(log)])
        self.assertEqual(rc, 0)
        stream.flush()
        written = stream.buffer.getvalue().decode("utf-8", "replace")
        self.assertIn("::error title=", written)
        self.assertIn("test_alpha", written)

    def test_an_unreconfigurable_stream_does_not_crash_it(self):
        """被换成别的对象（没有 reconfigure）时也得活着。"""
        class Plain:
            def __init__(self): self.text = ""
            def write(self, s): self.text += s
        plain = Plain()
        self.assertIs(ci_annotate.force_utf8_stdout(plain), plain)


class TheWorkflowUsesIt(unittest.TestCase):
    """workflow 与脚本得对得上：路径写错就等于没有报告器（已经发生过一次）。"""

    def setUp(self):
        self.text = (ROOT / ".github" / "workflows" / "ci.yml").read_text(
            encoding="utf-8")

    def test_both_steps_call_the_script(self):
        self.assertEqual(
            self.text.count("python scripts/ci_annotate.py"), 2,
            "单元测试与记分板两步都该接报告器")

    def test_the_log_path_is_relative_on_purpose(self):
        """Git Bash 的 /tmp 和 Windows python 的 /tmp 不是同一个地方。"""
        self.assertIn("tee unit.log", self.text)
        self.assertIn("tee parity.log", self.text)
        self.assertNotIn("/tmp/unit.log", self.text)

    def test_the_job_forces_utf8_for_python(self):
        """测试名与断言消息全是中文；Windows 的管道默认是本地代码页。"""
        self.assertIn("PYTHONIOENCODING: utf-8", self.text)

    def test_the_real_exit_code_survives(self):
        self.assertIn('exit "$rc"', self.text)
        self.assertIn("|| true", self.text)


class AHangLeavesAStack(unittest.TestCase):
    """挂住的用例要自己把栈打出来，而不是让 job 挂到上限被判 cancelled。

    2026-09-22：CI 的 macOS 那列单元测试步骤一直不结束（上一轮 cancelled）。
    一个挂住的 job 比一个红的 job 难查得多——它连结论都不给，而 job 日志又要
    仓库 admin 权限才下得到。
    """

    def arm(self, env):
        code = ("import sys; sys.path.insert(0, %r)\n"
                "import tests, time\n"
                "time.sleep(5)\n"
                "print('NOT REACHED')\n") % str(ROOT)
        return subprocess.run([sys.executable, "-c", code],
                              capture_output=True, encoding="utf-8", errors="replace", text=True, timeout=60,
                              env=dict(__import__("os").environ, **env))

    def test_ci_arms_the_watchdog_and_a_hang_dumps_threads(self):
        done = self.arm({"CI": "true", "ZYLAB_TEST_WATCHDOG": "1"})
        self.assertNotIn("NOT REACHED", done.stdout)
        self.assertIn("Timeout", done.stderr, done.stderr[-300:])
        self.assertIn("most recent call first", done.stderr)

    def test_a_local_run_is_not_armed(self):
        """本地跑测试不该被一个看门狗打断。"""
        done = self.arm({"CI": "", "ZYLAB_TEST_WATCHDOG": ""})
        self.assertIn("NOT REACHED", done.stdout, done.stderr[-300:])

    def test_the_watchdog_budget_is_under_the_job_timeout(self):
        """时限要小于 workflow 的 timeout-minutes，否则 job 先被杀、栈就没了。"""
        text = (ROOT / ".github" / "workflows" / "ci.yml").read_text(
            encoding="utf-8")
        import re
        minutes = int(re.search(r"timeout-minutes:\s*(\d+)", text).group(1))
        source = (ROOT / "tests" / "__init__.py").read_text(encoding="utf-8")
        seconds = int(re.search(r'"(\d+)" if os\.environ\.get\("CI"\)',
                                source).group(1))
        self.assertLess(seconds, minutes * 60,
                        "看门狗比 job 超时还晚，栈永远打不出来")


class AHangDumpIsReportedFromItsHead(unittest.TestCase):
    """faulthandler 是「最近调用在最前」——挂在哪儿要看开头，不是尾部。

    2026-09-22 macOS 那列：看门狗准时开火（20:02）、栈也打出来了，而注解里只有
    `unittest/main.py in runTests` —— 因为「尾部」那条取的是最后 8 行，
    那是 unittest 最外层的 runner 帧，一点信息都没有。
    """

    HANG_LOG = (
        "..F\n"
        "Timeout (0:20:00)!\n"
        "Thread 0x00007000 (most recent call first):\n"
        '  File "/x/tests/test_thing.py", line 42 in test_hang\n'
        '  File "/lib/unittest/case.py", line 100 in run\n'
        '  File "/lib/unittest/main.py", line 270 in runTests\n')

    def test_the_hang_annotation_starts_at_the_timeout_line(self):
        bodies = dict(ci_annotate.annotations(self.HANG_LOG))
        hang = next(v for k, v in bodies.items() if "挂起" in k)
        self.assertTrue(hang.startswith("Timeout ("), hang[:60])
        self.assertIn("test_hang", hang, "最内层那帧才是要看的")

    def test_a_normal_failure_log_has_no_hang_annotation(self):
        titles = [t for t, _ in ci_annotate.annotations(UNITTEST_LOG)]
        self.assertFalse([t for t in titles if "挂起" in t])


class WindowsCleanupIsTolerant(unittest.TestCase):
    """Windows 上删不掉还被打开着的文件是平台事实，不该把通过的用例记成 ERROR。

    2026-09-22 公开仓库 CI 的 Windows 那列：注解里列出的 40 条**全是 ERROR、
    没有一条 FAIL**，每条 traceback 的尾巴都一样——
    `TemporaryDirectory.__exit__ → _rmtree_unsafe → PermissionError [WinError 32]`。
    凡是起过子进程、或留着没关的 sqlite 连接的用例都会这样，于是整列读数
    无法解释：看不出哪些是真失败。
    """

    def test_the_patch_only_applies_to_windows(self):
        import tempfile
        patched = tempfile.TemporaryDirectory.__name__ == \
            "_TolerantTemporaryDirectory"
        self.assertEqual(patched, sys.platform == "win32",
                         "POSIX 上清理失败是真信号，不许吞")

    def test_it_defaults_to_ignoring_cleanup_errors(self):
        """无论在哪个平台，那个类本身的默认值得是「忽略清理错误」。"""
        import inspect
        import tempfile as real
        source = (Path(ROOT) / "tests" / "__init__.py").read_text(
            encoding="utf-8")
        self.assertIn('kwargs.setdefault("ignore_cleanup_errors", True)', source)
        self.assertIn('if sys.platform == "win32":', source)
        # 标准库得真的支持这个参数（3.10 起）
        self.assertIn("ignore_cleanup_errors",
                      inspect.signature(real.TemporaryDirectory).parameters)

    def test_a_still_open_file_does_not_break_cleanup_where_it_is_patched(self):
        import tempfile
        holder = tempfile.TemporaryDirectory(prefix="zylab-open-")
        stuck = Path(holder.name) / "held.txt"
        handle = open(stuck, "w", encoding="utf-8")
        handle.write("x")
        try:
            holder.cleanup()          # Windows 上这行原来会抛 WinError 32
        finally:
            handle.close()
            try:
                holder.cleanup()
            except OSError:
                pass



class TheExceptionClassHistogram(unittest.TestCase):
    """59 类根因、注解只放得下十来条 —— 剩下 44 类得有个说法。

    2026-09-22 Windows 那列就是这样：看得见 12 类，另外 44 类连形状都不知道。
    按异常类再收一层，一行就能说清「多少是断言失败、多少是 OSError」。
    """

    def test_it_strips_the_module_path_and_the_message(self):
        self.assertEqual(
            ci_annotate.exception_class(
                "core.checkpoints.ManifestError: root 路径含符号链接: /var"),
            "ManifestError")
        self.assertEqual(
            ci_annotate.exception_class("AssertionError: 1 != 2"),
            "AssertionError")

    def test_a_line_that_is_not_an_exception_is_labelled_as_such(self):
        """traceback 尾巴有时是一行输出残片，不该被当成异常类名。"""
        for line in ("", "   ", "+ external", "  ✻ 思考中 0s"):
            with self.subTest(line=line):
                self.assertEqual(
                    ci_annotate.exception_class(line), "（无异常行）")

    def test_the_histogram_counts_blocks_not_causes(self):
        """一类根因命中 12 次，直方图里就该是 12，而不是 1。"""
        causes = [("AssertionError: a", 12, "n1", "b1"),
                  ("AssertionError: b", 3, "n2", "b2"),
                  ("OSError: x", 1, "n3", "b3")]
        self.assertEqual(ci_annotate.class_histogram(causes),
                         "AssertionError\u00d715 · OSError\u00d71")

    def test_it_rides_along_in_the_summary(self):
        log = ("..E\n" + "=" * 70 + "\n"
               "ERROR: test_x (m.C.test_x)\n" + "-" * 70 + "\n"
               "Traceback (most recent call last):\n"
               "OSError: boom\n\n" + "-" * 70 + "\nRan 3 tests in 1s\n\n"
               "FAILED (errors=1)\n")
        body = dict(ci_annotate.annotations(log))["根因汇总"]
        self.assertIn("按异常类：OSError\u00d71", body)

if __name__ == "__main__":
    unittest.main()
