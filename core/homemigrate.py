"""状态目录的搬家与位置核对（自包含模式的底座）。

**零 core.* 依赖，只有 stdlib。** core/store.py 在 import 时就把模块级路径常量绑死到 state_home()，
所以任何"目录换位置"的处理都必须在入口的第一条 `from core import` 之前完成；要判活租约又不能
import store，于是这里自己拼 `session-leases` 路径、自己判 staleness。`LEASE_STALE_SECONDS` 与
store.SESSION_LEASE_STALE_SECONDS 是刻意的重复：改一处要改两处。

- relocate(old, new)：整体搬家——活租约（30 s 内心跳 + 进程在）则拒绝；os.rename（跨设备失败不自动
  copy，报清楚让用户手动 mv）；然后改写目录里 json/jsonl 记的旧绝对路径、记录新位置。
- reconcile_location(home)：目录被整体拷到别处后，按 .location 把旧绝对路径改写到新位置。
- 禁止 symlink。
"""
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

NEW_NAME = "zylab"
LEASE_STALE_SECONDS = 30.0   # == core/store.py SESSION_LEASE_STALE_SECONDS


def _home(environ):
    return Path(os.path.expanduser(environ.get("HOME") or "~"))


def new_home(environ=None):
    environ = os.environ if environ is None else environ
    return _home(environ) / f".{NEW_NAME}"


def portable_home(environ=None):
    """应用目录里的便携状态目录。与 core/paths.PORTABLE_DIRNAME / app_root() 对齐（刻意重复，改一处要改两处）。"""
    environ = os.environ if environ is None else environ
    override = environ.get(f"{NEW_NAME.upper()}_APP_ROOT")
    root = Path(os.path.expanduser(override)) if override else Path(__file__).resolve().parents[1]
    return root / f".{NEW_NAME}-home"


def current_home(environ=None):
    """与 core/paths.state_home() 同一套三级优先：显式改道 → 便携 → 默认。"""
    environ = os.environ if environ is None else environ
    override = environ.get(f"{NEW_NAME.upper()}_HOME")
    if override:
        return Path(os.path.expanduser(override))
    portable = portable_home(environ)
    if portable.is_dir():
        return portable
    return new_home(environ)


LOCATION_FILE = ".location"


def record_location(home):
    """记下状态目录此刻的绝对路径；下次启动位置变了就知道要改写目录里记的绝对路径。"""
    try:
        (Path(home) / LOCATION_FILE).write_text(str(Path(home)) + "\n", encoding="utf-8")
    except OSError:
        pass


def reconcile_location(home, *, out=None):
    """自包含目录被整体拷到别处后：把目录里 json/jsonl 记的旧绝对路径改写到新位置。返回改写的文件数。"""
    out = out or sys.stdout
    home = Path(home)
    if not home.is_dir():
        return 0
    marker = home / LOCATION_FILE
    try:
        recorded = marker.read_text(encoding="utf-8").strip() if marker.is_file() else ""
    except OSError:
        recorded = ""
    if not recorded:
        record_location(home)
        return 0
    if recorded == str(home):
        return 0
    rewritten, failed = rewrite_paths(home, recorded, str(home))
    record_location(home)
    out.write(f"  状态目录从 {recorded} 搬到了 {home}：改写了 {rewritten} 个文件里的旧路径\n")
    for item in failed:
        out.write(f"  ! 未能改写：{item}\n")
    return rewritten


def _pid_alive(pid):
    try:
        os.kill(int(pid), 0)
    except (OSError, TypeError, ValueError):
        return False
    return True


def live_leases(old, *, now=None):
    """旧目录下 30 s 内有心跳且进程仍在的租约 → [(pid, session_id)]。"""
    now = now or datetime.now(timezone.utc)
    found = []
    leases = old / "session-leases"
    if not leases.is_dir():
        return found
    for path in sorted(leases.glob("*.json")):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
            beat = datetime.fromisoformat(str(record.get("heartbeat_at")))
            if beat.tzinfo is None:
                beat = beat.replace(tzinfo=timezone.utc)
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            continue
        if (now - beat).total_seconds() > LEASE_STALE_SECONDS:
            continue
        pid = record.get("pid")
        if _pid_alive(pid):
            found.append((pid, record.get("session_id")))
    return found


def _measure(root):
    total = 0
    count = 0
    for entry in root.iterdir():
        count += 1
        if entry.is_file():
            total += entry.stat().st_size
        elif entry.is_dir():
            for sub in entry.rglob("*"):
                try:
                    if sub.is_file():
                        total += sub.stat().st_size
                except OSError:
                    pass
    return total, count


def _human(size):
    for unit in ("B", "K", "M", "G"):
        if size < 1024 or unit == "G":
            return f"{size:.0f}{unit}" if unit == "B" else f"{size:.1f}{unit}".replace(".0", "")
        size /= 1024


def rewrite_paths(root, old_prefix, new_prefix):
    """目录里 *.json / *.jsonl 中的旧绝对路径前缀 → 新前缀。tmp + os.replace，保留权限位。
    返回 (改写的文件数, 失败列表)。"""
    rewritten, failed = 0, []
    old_b, new_b = old_prefix.encode("utf-8"), new_prefix.encode("utf-8")
    for path in root.rglob("*"):
        if not path.is_file() or path.suffix not in (".json", ".jsonl"):
            continue
        try:
            data = path.read_bytes()
            if old_b not in data:
                continue
            tmp = path.with_name(path.name + ".migrate-tmp")
            tmp.write_bytes(data.replace(old_b, new_b))
            try:
                os.chmod(tmp, path.stat().st_mode & 0o777)
            except OSError:
                pass
            os.replace(tmp, path)
            rewritten += 1
        except OSError as exc:
            failed.append(f"{path}: {exc}")
    return rewritten, failed


def relocate(old, new, *, out=None, verb="已搬家"):
    """把整个状态目录从 old 搬到 new：活租约则拒绝（2）；os.rename（1 = 失败，不自动 copy）；
    然后改写目录里记的旧绝对路径、记录新位置。返回 0 表示成功。"""
    out = out or sys.stdout
    old, new = Path(old), Path(new)
    live = live_leases(old)
    if live:
        pids = ", ".join(f"pid {pid}（会话 {sid}）" for pid, sid in live)
        out.write(f"  ✗ 检测到活会话仍在使用 {old}：{pids}\n"
                  f"    请先退出旧进程再启动 {NEW_NAME}（状态目录要搬到 {new}）\n")
        return 2
    size, count = _measure(old)
    try:
        new.parent.mkdir(parents=True, exist_ok=True)
        os.rename(old, new)
    except OSError as exc:
        out.write(f"  ✗ 状态目录搬家失败：{old} → {new}（{exc}）\n"
                  f"    不自动复制。请手动执行：mv {old} {new}   （跨设备时：cp -a {old} {new} && rm -rf {old}）\n")
        return 1
    rewritten, failed = rewrite_paths(new, str(old), str(new))
    record_location(new)
    out.write(f"  {verb} {old} → {new}（{_human(size)}，{count} 项；改写了 {rewritten} 个文件里的旧路径）。"
              f"如需回退：mv {new} {old}\n")
    for item in failed:
        out.write(f"  ! 未能改写：{item}\n")
    return 0
