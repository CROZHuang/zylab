"""换行风格必须逐字保留（2026-09-17）。

换行是**文件的属性，不是平台的属性**。一个编码代理如果在编辑时顺手改掉整份
文件的换行，代价是：git 里变成全文件 diff（真正的改动淹没在噪音里）、
CRLF 的 `#!/bin/sh` 直接跑不起来、别人 review 时看不出你改了什么。

实测过的两个方向都要钉住：

* Windows 上 `Path.write_text()` 会把 `\\n` 翻成 `\\r\\n` —— 编辑 LF 文件里的
  一个词，整份文件被改成 CRLF。
* 反过来，POSIX 上把 CRLF 文件读进来（通用换行）再写回，会被压成 LF。

还有匹配侧：模型写的 `old`/`new` 一律用 `\\n`。不在匹配前归一化的话，
CRLF 文件上的 edit_file 永远「匹配 0 处」—— 在 Windows 上新建的文件基本都是
CRLF，等于 edit 工具直接不可用。
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = str(Path(__file__).resolve().parents[1])
sys.path.insert(0, ROOT)

from core import checkpoints, tools  # noqa: E402

CRLF = b"alpha\r\nbeta\r\ngamma\r\n"
LF = b"alpha\nbeta\ngamma\n"


class PreparedWritePreservesNewlines(unittest.TestCase):
    """会话里真正走的那条路（prepare_write → checkpoints 落盘）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def prepare(self, raw, **args):
        target = self.root / "sample.py"
        target.write_bytes(raw)
        return target, checkpoints.prepare_write(
            "edit_file", {"path": str(target), **args},
            workspace_root=str(self.root))

    def test_lf_file_stays_lf(self):
        _, prepared = self.prepare(LF, old="beta\n", new="BETA\n")
        self.assertEqual(prepared.match_count, 1)
        self.assertEqual(prepared.after_bytes, b"alpha\nBETA\ngamma\n")

    def test_crlf_file_stays_crlf(self):
        _, prepared = self.prepare(CRLF, old="beta\n", new="BETA\n")
        self.assertEqual(prepared.after_bytes, b"alpha\r\nBETA\r\ngamma\r\n")

    def test_model_writes_lf_and_still_matches_a_crlf_file(self):
        """模型不知道、也不该关心目标文件是 CRLF 还是 LF。"""
        _, prepared = self.prepare(CRLF, old="beta\n", new="BETA\n")
        self.assertEqual(prepared.match_count, 1, "CRLF 文件必须能被 \\n 的 old 匹配到")
        self.assertTrue(prepared.executable)

    def test_multi_line_replacement_keeps_the_style(self):
        _, prepared = self.prepare(
            CRLF, old="beta\ngamma\n", new="one\ntwo\nthree\n")
        self.assertEqual(
            prepared.after_bytes, b"alpha\r\none\r\ntwo\r\nthree\r\n")

    def test_write_file_on_an_lf_file_does_not_introduce_crlf(self):
        target = self.root / "plain.py"
        target.write_bytes(LF)
        prepared = checkpoints.prepare_write(
            "write_file", {"path": str(target), "content": "x\ny\n"},
            workspace_root=str(self.root))
        self.assertEqual(prepared.after_bytes, b"x\ny\n")


class DirectToolsPreserveNewlines(unittest.TestCase):
    """t_write_file / t_edit_file 这条直连路径（备份、legacy 调用点）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        self._cwd = os.getcwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, self._cwd)

    def test_edit_keeps_each_style(self):
        for name, raw in (("crlf.txt", CRLF), ("lf.txt", LF)):
            with self.subTest(name=name):
                target = self.root / name
                target.write_bytes(raw)
                tools.t_edit_file(str(target), "beta", "BETA")
                self.assertEqual(
                    target.read_bytes(), raw.replace(b"beta", b"BETA"))

    def test_new_file_is_written_verbatim(self):
        target = self.root / "new.txt"
        tools.t_write_file(str(target), "one\ntwo\n")
        self.assertEqual(target.read_bytes(), b"one\ntwo\n")

    def test_overwriting_keeps_the_existing_style(self):
        target = self.root / "keep.txt"
        target.write_bytes(CRLF)
        tools.t_write_file(str(target), "x\ny\n")
        self.assertEqual(target.read_bytes(), b"x\r\ny\r\n")


class ReadingSeesTrueBytes(unittest.TestCase):
    """os.open 在 Windows 上默认是**文本模式**，会把 CRLF 悄悄折成 LF。"""

    def test_snapshot_length_matches_on_disk_size(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "sample.py"
            target.write_bytes(CRLF)
            prepared = checkpoints.prepare_write(
                "edit_file", {"path": str(target), "old": "beta", "new": "b"},
                workspace_root=tmp)
            self.assertEqual(
                len(prepared.before_bytes), target.stat().st_size,
                "读到的字节数必须等于磁盘上的大小，否则就是被换行翻译动过了")


if __name__ == "__main__":
    unittest.main()
