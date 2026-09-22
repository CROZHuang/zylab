"""自包含模式：graft 缓存可以在状态目录里（它就在仓库内），但仓库里其他位置仍拒绝。

2026-09-04：默认缓存 .zylab-home/graft 被"缓存必须在项目外"拒掉，模型转而改用户 settings
写 cache_root 绕过。规则改为：状态目录内放行；状态目录本身不参与索引。
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from tests.platform_support import (  # noqa: E402
    requires_posix_modes)
from core import graft, paths, store


class GraftCacheStateDirTests(unittest.TestCase):
    # graft 的信任检查全靠 POSIX 权限位（属主 + group/other 不可写）。
    # Windows 上 `os.stat().st_mode` 是合成值（目录一律 0o777），
    # `S_IWGRP|S_IWOTH` 永远置位，于是这套检查**恒不成立** —— 夹具造不出
    # 「私有目录」这个载体。而 graft 本身要 unshare + /proc/self/ns/net，
    # 在 Windows 上根本不可用，所以这里是 skip 而不是放宽产品侧的判据。
    @requires_posix_modes
    def test_state_dir_inside_repo_is_an_allowed_cache_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "app"
            state = repo / paths.PORTABLE_DIRNAME
            state.mkdir(parents=True)
            with mock.patch.object(store, "HOME", state):
                cache = graft.cache_dir(repo.resolve(), {"cache_root": None}, create=True)
            self.assertTrue(str(cache).startswith(str((state / "graft").resolve())))
            self.assertTrue(cache.exists())

    def test_other_locations_inside_the_repo_are_still_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp) / "app"
            (repo / paths.PORTABLE_DIRNAME).mkdir(parents=True)
            with mock.patch.object(store, "HOME", repo / paths.PORTABLE_DIRNAME):
                with self.assertRaisesRegex(graft.GraftError, "项目外"):
                    graft.cache_dir(repo.resolve(), {"cache_root": str(repo / "cache")}, create=False)

    def test_state_dir_is_never_indexed(self):
        self.assertIn(paths.PORTABLE_DIRNAME, graft.SKIP_DIRS)


if __name__ == "__main__":
    unittest.main()
