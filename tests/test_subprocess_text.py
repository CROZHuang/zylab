"""解码子进程输出**不许**让平台替我们挑编码（2026-09-22 公开仓库 CI 的 Windows 两列）。

事故：`test_cold_start` 里 `p.stdout` 是 `None` 而 `p.stderr` 是字符串——
`capture_output=True` 却只捕到一半。追进 CPython 才看清这条链：

1. `text=True` 不带 `encoding` 就用 **locale 编码**解码。Windows runner 上那是
   cp1252，而子进程的 `PYTHONIOENCODING=utf-8` 把中文按 UTF-8 写出去；
2. UTF-8 的续字节里正好有 cp1252 的未定义槽位（0x81/0x8D/0x8F/0x90/0x9D），
   读取线程里的 `fh.read()` 抛 `UnicodeDecodeError`，**线程死掉**；
3. `subprocess.py` 的 Windows 分支最后一行是
   `stdout = stdout[0] if stdout else None` —— 空 buffer 于是变成 **None**，
   不是 `""`，也没有任何人报错。

stderr 当时没事，只是因为那几次 stderr 里恰好全是 ASCII。

**产品后果比测试严重得多**：`core/store.py` 拿 `git status --porcelain` 判工作区
是否干净——仓库里只要有一个中文文件名，Windows 上 stdout 就整个变成 None，
zylab 会以为工作区是干净的。`core/hooks.py`、`core/repomap.py`、`core/agent.py`
同理。这类失败**安静**，所以只能靠规则挡。

规则：凡是要把子进程输出当文本读的调用，必须显式写出 `encoding=`。
用 AST 判，不用正则——`text=True` 也出现在 `tempfile.mkstemp(text=True)` 里，
那是文件模式标志，不是解码。
"""
import ast
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import tests  # noqa: F401,E402  —— 状态目录隔离

CALLS = {"run", "Popen", "check_output", "call", "check_call"}
TEXT_KW = {"text", "universal_newlines"}
# 有这些关键字才说明真的在读/写管道（区别于 mkstemp 的 text=True）
PIPE_KW = {"capture_output", "stdout", "stderr", "stdin", "input"}


def scan(path):
    """返回这个文件里所有「解码文本却没写 encoding」的调用行号。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    bad = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = (func.attr if isinstance(func, ast.Attribute)
                else func.id if isinstance(func, ast.Name) else None)
        if name not in CALLS:
            continue
        keywords = {kw.arg for kw in node.keywords if kw.arg}
        if not keywords & TEXT_KW or not keywords & PIPE_KW:
            continue
        if "encoding" not in keywords:
            bad.append(node.lineno)
    return bad


class EveryTextPipeNamesItsEncoding(unittest.TestCase):
    def sources(self):
        yield ROOT / "zylab.py"
        for folder in ("core", "scripts", "tests"):
            yield from sorted((ROOT / folder).glob("*.py"))

    def test_no_call_lets_the_platform_pick(self):
        offenders = []
        for path in self.sources():
            for line in scan(path):
                offenders.append(f"{path.relative_to(ROOT)}:{line}")
        self.assertEqual(
            offenders, [],
            "这些调用把编码交给了平台：Windows 上会按本地代码页解码 UTF-8，"
            "读取线程抛 UnicodeDecodeError 之后 stdout 变成 None（不是空串，"
            "也不报错）。显式写 encoding=\"utf-8\", errors=\"replace\"。")

    def test_the_scanner_can_tell_the_two_text_kwargs_apart(self):
        """mkstemp(text=True) 不是解码，不能误报；漏报同样不行。"""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            sample = Path(tmp) / "s.py"
            sample.write_text(
                "import subprocess, tempfile\n"
                "tempfile.mkstemp(text=True)\n"                      # 不算
                "subprocess.run(['x'], text=True)\n"                 # 不算（没管道）
                "subprocess.run(['x'], capture_output=True, text=True)\n"   # 算
                "subprocess.run(['x'], capture_output=True, text=True,\n"
                "               encoding='utf-8')\n",                # 不算
                encoding="utf-8")
            self.assertEqual(scan(sample), [4])


class TheDecodeActuallyRoundTrips(unittest.TestCase):
    """规则之外再要一条行为用例：显式 utf-8 真能把中文原样带回来。"""

    def test_chinese_on_stdout_survives(self):
        code = ("import sys; sys.stdout.reconfigure(encoding='utf-8');"
                "print('网关还没有 API key')")
        done = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIsNotNone(done.stdout, "捕获失败会是 None 而不是空串")
        self.assertIn("网关还没有 API key", done.stdout)

    def test_undecodable_bytes_degrade_but_never_vanish(self):
        """errors='replace' 的意义：坏字节变成替换符，而不是整条 stdout 消失。"""
        code = (r"import sys; sys.stdout.buffer.write(b'A\xff\xfeB')")
        done = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=60)
        self.assertIsNotNone(done.stdout)
        self.assertTrue(done.stdout.startswith("A"), done.stdout)
        self.assertTrue(done.stdout.endswith("B"), done.stdout)



class RenameOverwritesOnlyOnPosix(unittest.TestCase):
    """`os.rename` 覆盖已存在的目标，**只有 POSIX 成立**。

    Windows 的 rename 不覆盖，直接 `FileExistsError [WinError 183]`。
    2026-09-22 两列 Windows 都栽在同一处：会话迁移刻意是幂等的，第二次跑必然
    撞上已存在的 `last.<sid>.json.migrated`，于是「再跑一次」在 Linux 上成功、
    在 Windows 上炸（`core/store.py` 已改成 `os.replace`）。

    **这条差异在 Linux 上一行都测不出来**（那边就是成功），所以规则钉在源码层：
    产品代码里每一处 `rename` 都要在下面这份名单里，新增一处就得先回答
    「目标可能已存在吗」——可能就用 `os.replace`。
    """

    ALLOWED = {
        # 整个状态目录搬家：目标已存在时**就该失败**，不能把用户既有的状态目录
        # 盖掉。这里 Windows 更严格反而是对的，刻意保留 rename。
        "core/homemigrate.py",
    }

    def test_every_rename_is_a_deliberate_one(self):
        found = {}
        for path in [ROOT / "zylab.py"] + sorted((ROOT / "core").glob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            lines = [node.lineno for node in ast.walk(tree)
                     if isinstance(node, ast.Call)
                     and isinstance(node.func, ast.Attribute)
                     and node.func.attr == "rename"]
            if lines:
                found[str(path.relative_to(ROOT))] = lines
        self.assertEqual(
            sorted(found), sorted(self.ALLOWED),
            "多出来的 rename 要先回答「目标可能已存在吗」：可能就用 os.replace"
            "（POSIX 上两者同义，Windows 上只有 replace 会覆盖）；"
            f"实际找到 {found}")

if __name__ == "__main__":
    unittest.main()
