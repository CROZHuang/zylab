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
                         ["失败清单", "traceback 1", "traceback 2", "尾部"])
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
        return subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "ci_annotate.py"), *args],
            capture_output=True, text=True, timeout=60)

    def test_it_prints_error_commands(self):
        log = self.tmp / "unit.log"
        log.write_text(UNITTEST_LOG, encoding="utf-8")
        done = self.run_it(str(log))
        self.assertEqual(done.returncode, 0, done.stderr)
        lines = [l for l in done.stdout.splitlines() if l.startswith("::error")]
        self.assertGreaterEqual(len(lines), 3)
        for line in lines:
            self.assertNotIn("\t", line)

    def test_a_missing_log_warns_but_never_masks_the_real_failure(self):
        """报告器自己坏掉，不该把被报告的那个失败盖住——所以退出码是 0。"""
        done = self.run_it(str(self.tmp / "nope.log"))
        self.assertEqual(done.returncode, 0, done.stderr)
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
                              capture_output=True, text=True, timeout=60,
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


if __name__ == "__main__":
    unittest.main()
