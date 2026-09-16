"""zylab 的版本标识：tarball 里是烤进去的，git 克隆里现问 git。

为什么需要它：同事解压/克隆之后用上几周再报 bug，维护者无从知道他跑的是哪一版
（2026-09-15 实测 `--version` 直接 argparse 报错退出 2，仓库 0 个 tag）。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from . import _build

_PLACEHOLDER = "$Format:"


def _from_archive():
    commit = str(getattr(_build, "COMMIT", "") or "")
    if not commit or commit.startswith(_PLACEHOLDER):
        return None
    date = str(getattr(_build, "DATE", "") or "")
    return {
        "commit": commit,
        "date": "" if date.startswith(_PLACEHOLDER) else date,
        "source": "tarball",
    }


def _from_git():
    try:
        # 问「代码自己在哪」，不是问状态目录：ZYLAB_APP_ROOT 可以被改道到
        # 临时目录（测试就这么干），那时 app_root() 根本不是这个仓库。
        repo = Path(__file__).resolve().parent.parent
        done = subprocess.run(
            ["git", "-C", str(repo), "log", "-1", "--format=%H%n%cI"],
            capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.SubprocessError):
        return None
    if done.returncode != 0:
        return None
    lines = [line.strip() for line in done.stdout.splitlines() if line.strip()]
    if not lines:
        return None
    return {
        "commit": lines[0],
        "date": lines[1] if len(lines) > 1 else "",
        "source": "git",
    }


def resolve():
    return (_from_archive() or _from_git()
            or {"commit": "", "date": "", "source": "unknown"})


def render():
    """一行版本串；报 bug 时贴这一行就够维护者定位。"""
    info = resolve()
    commit = info["commit"][:12] or "unknown"
    date = info["date"][:10]
    python = "%d.%d.%d" % sys.version_info[:3]
    parts = [f"zylab {commit}"]
    if date:
        parts.append(date)
    parts.append(f"python {python}")
    parts.append(info["source"])
    return " · ".join(parts)
