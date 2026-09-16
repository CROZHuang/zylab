"""v1 JSON/JSONL 到 StateStore 的无损迁移。

默认入口只做 dry-run。写迁移要求显式 target 和 write=True，并且：
- target 已存在时拒绝；
- 临时库使用 DELETE journal；
- 任一解析/完整性错误都不切换 target；
- 原 session JSON 与 usage.jsonl 从不删除、移动或覆盖。
"""
from __future__ import annotations
from . import paths

import argparse
import json
import os
import sqlite3
from dataclasses import asdict, dataclass, field
from pathlib import Path

from .state import (ImportConflict, StateError, StateStore, canonical_json,
                    sha256_bytes)


class MigrationFailed(StateError):
    def __init__(self, message, report=None):
        super().__init__(message)
        self.report = report


@dataclass
class MigrationReport:
    dry_run: bool
    sessions_dir: str
    usage_path: str
    target_path: str | None = None
    session_files_seen: int = 0
    sessions_valid: int = 0
    sessions_imported: int = 0
    sessions_skipped: int = 0
    messages_seen: int = 0
    messages_valid: int = 0
    messages_imported: int = 0
    usage_files_seen: int = 0
    usage_rows_seen: int = 0
    usage_rows_valid: int = 0
    usage_rows_imported: int = 0
    usage_rows_skipped: int = 0
    target_created: bool = False
    journal_mode: str | None = None
    integrity: list[str] = field(default_factory=list)
    foreign_key_violations: list[list] = field(default_factory=list)
    manifests: list[dict] = field(default_factory=list)
    errors: list[dict] = field(default_factory=list)

    @property
    def ok(self):
        return not self.errors and (
            not self.integrity or self.integrity == ["ok"]
        ) and not self.foreign_key_violations

    def add_error(self, source, error, *, line=None, raw_excerpt=None):
        issue = {"source": str(source), "error": str(error)}
        if line is not None:
            issue["line"] = int(line)
        if raw_excerpt is not None:
            issue["raw_excerpt"] = str(raw_excerpt)[:200]
        self.errors.append(issue)
        return issue

    def as_dict(self):
        out = asdict(self)
        out["ok"] = self.ok
        return out


def _session_paths(sessions_dir):
    root = Path(sessions_dir)
    if not root.is_dir():
        return []
    return sorted(p for p in root.glob("*.json") if p.is_file())


def _read_session(path):
    path = Path(path)
    data = path.read_bytes()
    digest = sha256_bytes(data)
    try:
        rec = json.loads(data.decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise ValueError(f"不是 UTF-8：{exc}") from None
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"JSON 错误 line {exc.lineno} column {exc.colno}: {exc.msg}") from None
    if not isinstance(rec, dict):
        raise ValueError("顶层必须是 JSON object")
    messages = rec.get("messages")
    if not isinstance(messages, list):
        raise ValueError("messages 必须是 list")
    for i, message in enumerate(messages):
        if not isinstance(message, dict):
            raise ValueError(f"messages[{i}] 必须是 object")
        canonical_json(message)
    mapped = dict(rec)
    mapped["id"] = str(rec.get("id") or path.stem)
    return mapped, messages, digest, len(data)


def _read_usage(path):
    path = Path(path)
    data = path.read_bytes()
    digest = sha256_bytes(data)
    rows, errors = [], []
    seen = 0
    for line_no, raw in enumerate(data.splitlines(), 1):
        if not raw.strip():
            continue
        seen += 1
        try:
            text = raw.decode("utf-8")
            rec = json.loads(text)
            if not isinstance(rec, dict):
                raise ValueError("usage row 必须是 JSON object")
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            errors.append({
                "line": line_no,
                "error": str(exc),
                "raw_excerpt": raw.decode("utf-8", "replace")[:200],
            })
            continue
        rows.append((line_no, rec))
    return rows, errors, digest, len(data), seen


def _manifest(path, kind, digest, size, rows):
    return {
        "source_path": str(Path(path).absolute()),
        "source_kind": kind,
        "sha256": digest,
        "bytes": int(size),
        "rows": int(rows),
    }


def plan_v1_migration(sessions_dir, usage_path, *, target_path=None):
    """只读扫描 legacy sources，不创建数据库或 lock。"""
    report = MigrationReport(
        dry_run=True,
        sessions_dir=str(Path(sessions_dir)),
        usage_path=str(Path(usage_path)),
        target_path=str(Path(target_path)) if target_path else None,
    )
    for path in _session_paths(sessions_dir):
        report.session_files_seen += 1
        try:
            _rec, messages, digest, size = _read_session(path)
        except (OSError, ValueError, TypeError) as exc:
            report.add_error(path, exc)
            continue
        report.sessions_valid += 1
        report.messages_seen += len(messages)
        report.messages_valid += len(messages)
        report.manifests.append(
            _manifest(path, "session", digest, size, len(messages)))

    usage = Path(usage_path)
    if usage.is_file():
        report.usage_files_seen = 1
        try:
            rows, errors, digest, size, seen = _read_usage(usage)
        except OSError as exc:
            report.add_error(usage, exc)
        else:
            report.usage_rows_seen = seen
            report.usage_rows_valid = len(rows)
            for issue in errors:
                report.add_error(
                    usage, issue["error"], line=issue["line"],
                    raw_excerpt=issue["raw_excerpt"])
            report.manifests.append(
                _manifest(usage, "usage", digest, size, seen))
    return report


def import_v1(store, sessions_dir, usage_path):
    """导入到已打开的 StateStore。调用者决定它是临时库还是 shadow 库。"""
    report = MigrationReport(
        dry_run=False,
        sessions_dir=str(Path(sessions_dir)),
        usage_path=str(Path(usage_path)),
        target_path=str(store.path),
        journal_mode=store.journal_mode,
    )
    for path in _session_paths(sessions_dir):
        report.session_files_seen += 1
        try:
            rec, messages, digest, size = _read_session(path)
        except (OSError, ValueError, TypeError) as exc:
            report.add_error(path, exc)
            continue
        report.sessions_valid += 1
        report.messages_seen += len(messages)
        report.messages_valid += len(messages)
        report.manifests.append(
            _manifest(path, "session", digest, size, len(messages)))
        try:
            result = store.import_legacy_session(
                rec, messages,
                source_path=str(path.absolute()),
                source_sha256=digest,
                source_size=size)
        except (StateError, ImportConflict, ValueError, sqlite3.Error) as exc:
            report.add_error(path, exc)
            continue
        if result["skipped"]:
            report.sessions_skipped += 1
        else:
            report.sessions_imported += 1
            report.messages_imported += result["messages"]

    usage = Path(usage_path)
    if usage.is_file():
        report.usage_files_seen = 1
        try:
            rows, errors, digest, size, seen = _read_usage(usage)
        except OSError as exc:
            report.add_error(usage, exc)
        else:
            report.usage_rows_seen = seen
            report.usage_rows_valid = len(rows)
            report.manifests.append(
                _manifest(usage, "usage", digest, size, seen))
            for issue in errors:
                report.add_error(
                    usage, issue["error"], line=issue["line"],
                    raw_excerpt=issue["raw_excerpt"])
            try:
                result = store.import_legacy_usage(
                    rows, errors,
                    source_path=str(usage.absolute()),
                    source_sha256=digest,
                    source_size=size,
                    rows_seen=seen)
            except (StateError, ImportConflict, ValueError, sqlite3.Error) as exc:
                report.add_error(usage, exc)
            else:
                if result["skipped"]:
                    report.usage_rows_skipped += len(rows)
                else:
                    report.usage_rows_imported += result["rows"]

    report.integrity = store.integrity_check()
    report.foreign_key_violations = [
        list(v) for v in store.foreign_key_violations()]
    if report.integrity != ["ok"]:
        report.add_error(store.path, "integrity_check: " + repr(report.integrity))
    if report.foreign_key_violations:
        report.add_error(
            store.path,
            "foreign_key_check: " + repr(report.foreign_key_violations[:5]))
    return report


def _reject_protected_target(path):
    resolved = Path(path).resolve(strict=False)
    if paths.under_protected(resolved):
        raise MigrationFailed(f"迁移 target 不能位于受保护路径：{resolved}")


def _fsync_file(path):
    with open(path, "rb") as handle:
        os.fsync(handle.fileno())


def _fsync_dir(path):
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    fd = os.open(path, flags)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _write_error_manifest(path, report):
    """尽力写诊断；失败不能覆盖原 migration error。"""
    try:
        payload = json.dumps(
            report.as_dict(), ensure_ascii=False, indent=2) + "\n"
        tmp = Path(str(path) + ".tmp")
        tmp.write_text(payload, encoding="utf-8")
        os.chmod(tmp, 0o600)
        tmp.replace(path)
    except OSError:
        pass


def migrate_v1_to_new_db(sessions_dir, usage_path, target_path, *,
                         dry_run=True, _stage_hook=None):
    """对新 target 执行 crash-safe 迁移；默认只 dry-run。"""
    target = Path(target_path)
    _reject_protected_target(target)
    if dry_run:
        return plan_v1_migration(
            sessions_dir, usage_path, target_path=target)
    if target.exists():
        raise MigrationFailed(f"target 已存在，拒绝覆盖：{target}")
    created_parent = not target.parent.exists()
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if created_parent:
        os.chmod(target.parent, 0o700)
    temp = Path(str(target) + ".tmp")
    lock = Path(str(target) + ".migrate.lock")
    if temp.exists():
        raise MigrationFailed(f"临时库已存在，先检查而不是覆盖：{temp}")

    try:
        lock_fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        raise MigrationFailed(f"已有 migration lock：{lock}") from None
    try:
        os.write(lock_fd, f"pid={os.getpid()}\n".encode())
    finally:
        os.close(lock_fd)

    report = None
    try:
        with StateStore(temp, journal_mode="DELETE") as store:
            report = import_v1(store, sessions_dir, usage_path)
            if not report.ok:
                _write_error_manifest(
                    Path(str(temp) + ".errors.json"), report)
                raise MigrationFailed(
                    f"legacy 数据有 {len(report.errors)} 个错误，未切换 target",
                    report)
            if _stage_hook is not None:
                _stage_hook("after_import", temp, target, report)

        for suffix in ("-wal", "-shm"):
            if Path(str(temp) + suffix).exists():
                raise MigrationFailed(
                    f"临时库关闭后仍有 {suffix}，拒绝 rename", report)
        _fsync_file(temp)
        _fsync_dir(target.parent)
        if target.exists():
            raise MigrationFailed(f"target 在迁移期间出现，拒绝覆盖：{target}",
                                  report)
        try:
            # link 在同一目录内原子创建且绝不覆盖已有 target；随后移除临时名。
            # 比 exists + rename 更安全，后者存在 TOCTOU 覆盖窗口。
            os.link(temp, target)
        except FileExistsError:
            raise MigrationFailed(
                f"target 在迁移期间出现，拒绝覆盖：{target}", report) from None
        _fsync_dir(target.parent)
        temp.unlink()
        _fsync_dir(target.parent)
        with StateStore(target, journal_mode="WAL") as final:
            report.integrity = final.integrity_check()
            report.foreign_key_violations = [
                list(v) for v in final.foreign_key_violations()]
            report.journal_mode = final.journal_mode
        report.target_path = str(target)
        report.target_created = True
        if report.integrity != ["ok"] or report.foreign_key_violations:
            raise MigrationFailed("最终数据库完整性检查失败", report)
        return report
    finally:
        try:
            lock.unlink()
        except OSError:
            # 清理失败不能覆盖真正的迁移结果；残留 lock 会让下次安全拒绝。
            pass


def main(argv=None):
    home = paths.state_home()
    parser = argparse.ArgumentParser(
        description="Zylab v1 JSON/JSONL -> v2 SQLite migration")
    parser.add_argument("--sessions", default=str(home / "sessions"))
    parser.add_argument("--usage", default=str(home / "usage.jsonl"))
    parser.add_argument("--target")
    parser.add_argument(
        "--write", action="store_true",
        help="实际创建 target；默认只做 read-only dry-run")
    args = parser.parse_args(argv)
    if args.write and not args.target:
        parser.error("--write 必须同时给 --target")
    target = args.target or str(home / "state.sqlite3")
    try:
        report = migrate_v1_to_new_db(
            args.sessions, args.usage, target, dry_run=not args.write)
    except MigrationFailed as exc:
        if exc.report:
            print(json.dumps(exc.report.as_dict(), ensure_ascii=False, indent=2))
        else:
            print(json.dumps({"ok": False, "error": str(exc)},
                             ensure_ascii=False, indent=2))
        return 2
    print(json.dumps(report.as_dict(), ensure_ascii=False, indent=2))
    return 0 if report.ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
