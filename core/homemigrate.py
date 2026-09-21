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
        _repair_once(home, out=out)
        return 0
    if recorded == str(home):
        _repair_once(home, out=out)
        return 0
    rewritten, failed = rewrite_paths(home, recorded, str(home))
    record_location(home)
    _repair_once(home, out=out, force=True)
    out.write(f"  状态目录从 {recorded} 搬到了 {home}：改写了 {rewritten} 个文件里的旧路径\n")
    for item in failed:
        out.write(f"  ! 未能改写：{item}\n")
    return rewritten


REPAIR_MARKER = ".escape-repair-v1"


def _repair_once(home, *, out=None, force=False):
    """对每个状态目录只做一次损坏扫描（marker 记账），迁移之后强制再做一次。

    全量扫描要读遍所有 json/jsonl，不该每次启动都付这个代价；而已经被写坏的
    目录又必须有人去修，否则会话列表和历史就一直是空的。
    """
    home = Path(home)
    marker = home / REPAIR_MARKER
    if marker.exists() and not force:
        return 0
    repaired = repair_bad_escapes(home, out=out)
    try:
        marker.write_text("1\n", encoding="utf-8")
    except OSError:
        pass
    return repaired


def _pid_alive(pid):
    """进程存活探测。与 core/wincompat.pid_alive 是刻意的重复：本模块必须
    零 core.* 依赖（它在第一条 `from core import` 之前就要跑），改一处要改两处。

    Windows 上**不能**用 os.kill(pid, 0)：那里的 0 是 CTRL_C_EVENT，
    既误判存活，又会向共享控制台的进程组真的送出 Ctrl+C。
    """
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.argtypes = [
            ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
        k32.OpenProcess.restype = ctypes.c_void_p
        handle = k32.OpenProcess(0x1000, False, pid)   # QUERY_LIMITED_INFORMATION
        if not handle:
            return ctypes.get_last_error() == 5        # ACCESS_DENIED = 存在
        try:
            k32.GetExitCodeProcess.argtypes = [
                ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
            code = ctypes.c_uint32()
            if not k32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return True
            return code.value == 259                   # STILL_ACTIVE
        finally:
            k32.CloseHandle(ctypes.c_void_p(handle))
    try:
        os.kill(pid, 0)
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


def _json_escaped(text):
    """路径在 JSON 字符串字面量里的样子（json.dumps 去掉首尾引号）。

    POSIX 路径里没有反斜杠，escaped == raw，所以 POSIX 上这层是恒等变换、
    行为一字不变。Windows 路径里的 `\\` 在 JSON 里必须写成 `\\\\` ——
    忘了这件事就是下面那条注释说的那次数据损坏。
    """
    return json.dumps(text)[1:-1]


def rewrite_paths(root, old_prefix, new_prefix):
    """目录里 *.json / *.jsonl 中的旧绝对路径前缀 → 新前缀。tmp + os.replace，保留权限位。
    返回 (改写的文件数, 失败列表)。

    **必须按 JSON 转义后的形态替换。** 早先这里直接对原始字节做 replace，
    于是把 Linux 路径 `/old/.zylab-home`（JSON 里无需转义）换成 Windows 路径
    `D:\\zylab\\.zylab-home` 时，裸反斜杠被塞进了 JSON 字符串字面量，
    `\\z` / `\\.` 都不是合法转义 —— 2026-09-17 实测：一次 Linux→Windows 迁移
    把 149 个状态文件里的 44 个（sessions / workflows / checkpoints /
    agent-runs / history.jsonl）变成了无法解析的 JSON。
    """
    rewritten, failed = 0, []
    # 先按 JSON 转义形态替换（正常写入的文件长这样），再兜一次原始形态：
    # 后者只会命中「上一版 bug 已经写坏的内容」，顺带把它修回合法 JSON。
    pairs = []
    for old, new in ((_json_escaped(old_prefix), _json_escaped(new_prefix)),
                     (old_prefix, _json_escaped(new_prefix))):
        pair = (old.encode("utf-8"), new.encode("utf-8"))
        if pair not in pairs:
            pairs.append(pair)
    for path in root.rglob("*"):
        if not path.is_file() or path.suffix not in (".json", ".jsonl"):
            continue
        try:
            data = path.read_bytes()
            if not any(old_b in data for old_b, _ in pairs):
                continue
            updated = data
            for old_b, new_b in pairs:
                updated = updated.replace(old_b, new_b)
            if updated == data:
                continue
            tmp = path.with_name(path.name + ".migrate-tmp")
            tmp.write_bytes(updated)
            try:
                os.chmod(tmp, path.stat().st_mode & 0o777)
            except OSError:
                pass
            os.replace(tmp, path)
            rewritten += 1
        except OSError as exc:
            failed.append(f"{path}: {exc}")
    return rewritten, failed


# JSON 里 `\` 之后只有这几个字符是合法转义；其余都说明这个反斜杠本该是字面量。
_VALID_JSON_ESCAPE = set('"\\/bfnrtu')


def _escape_lone_backslashes(text):
    out = []
    index = 0
    length = len(text)
    while index < length:
        char = text[index]
        out.append(char)
        if char == "\\":
            nxt = text[index + 1] if index + 1 < length else ""
            if nxt in _VALID_JSON_ESCAPE and nxt != "":
                out.append(nxt)
                index += 2
                continue
            out.append("\\")          # 裸反斜杠 → 补成合法转义
        index += 1
    return "".join(out)


def _parses_as(path, text):
    try:
        if path.suffix == ".jsonl":
            for line in text.splitlines():
                if line.strip():
                    json.loads(line)
        else:
            json.loads(text)
    except ValueError:
        return False
    return True


def repair_bad_escapes(root, *, out=None):
    """修复被上一版 rewrite_paths 写坏的 JSON（裸反斜杠 → 合法转义）。

    只碰**当前已经解析不了**的文件，而且只在修完能解析时才落盘 —— 修不好就
    原样留着，不会把一个坏文件换成另一个坏文件。返回修好的文件数。
    """
    out = out or sys.stdout
    root = Path(root)
    if not root.is_dir():
        return 0
    repaired = 0
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix not in (".json", ".jsonl"):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        if _parses_as(path, text):
            continue
        fixed = _escape_lone_backslashes(text)
        if fixed == text or not _parses_as(path, fixed):
            continue
        try:
            backup = path.with_name(path.name + ".badescape-bak")
            if not backup.exists():
                backup.write_text(text, encoding="utf-8")
            tmp = path.with_name(path.name + ".repair-tmp")
            tmp.write_text(fixed, encoding="utf-8")
            try:
                os.chmod(tmp, path.stat().st_mode & 0o777)
            except OSError:
                pass
            os.replace(tmp, path)
            repaired += 1
        except OSError as exc:
            out.write(f"  ! 未能修复 {path}：{exc}\n")
    if repaired:
        out.write(f"  修复了 {repaired} 个被旧版路径改写损坏的状态文件"
                  f"（原文件留在同名 .badescape-bak）\n")
    return repaired


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
