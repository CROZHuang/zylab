"""Deterministic ``@file`` and image attachments for provider requests.

The canonical transcript stores the user's exact text plus a private, hashed
snapshot manifest.  File bodies and image base64 are materialized only in the
ephemeral provider projection, so session JSON stays auditable and compact.
"""
from __future__ import annotations
from . import paths

import base64
import copy
import hashlib
import os
import re
import tempfile
from pathlib import Path

from . import store


ROOT = store.HOME / "attachments"
MAX_ATTACHMENTS = 8
MAX_TEXT_SOURCE_BYTES = 20 * 1024 * 1024
MAX_TEXT_CHARS = 200_000
MAX_TOTAL_TEXT_CHARS = 400_000
MAX_IMAGE_BYTES = 20 * 1024 * 1024
IMAGE_TOKEN_ESTIMATE = 2_048
IMAGE_TOKEN_ESTIMATOR = "image-input/2048-per-image-conservative-v1"
_TOKEN = re.compile(
    r"(?<!\S)@(?:\"([^\"\n]+)\"|'([^'\n]+)'|([^\s]+))")
_INTERNAL_FIELDS = frozenset({"_kc_attachments", "_kc_raw_input"})
_SENSITIVE_NAMES = frozenset({
    ".netrc", ".npmrc", ".pgpass", "credentials", "credentials.json",
    "id_rsa", "id_ed25519", "id_ecdsa", "clusterx.yaml", "keys.env",
    "rclone.conf", "auth.json", "secrets.yaml", "secrets.yml",
    "secrets.json", "kubeconfig",
})
_SENSITIVE_SUFFIXES = (".pem", ".key", ".p12", ".pfx", ".keystore", ".jks")
_SENSITIVE_DIRS = frozenset({".ssh", ".aws", ".gnupg", ".kube", ".docker"})

# 行级 secret 赋值：TOKEN=sk-…、api_key: "…" 之类。宁可多遮 —— 被遮的是
# 送往外部模型的副本，原文件不动。
_SECRET_LINE = re.compile(
    r"(?i)\b(secret|token|password|passwd|api[_-]?key|access[_-]?key|"
    r"private[_-]?key|authorization|bearer)\b(\s*[:=]\s*)(\S.*)")


def is_sensitive_path(path):
    """路径是否指向潜在凭据。返回命中原因（str）或 None。

    attachments（@file）、repo-map 发现器共用这一道闸 —— 分头各写一套
    清单必然漂移（repo-map 首版就因为没复用这里，把 credentials.json
    的内容送去了 provider，见 REVIEW-repo-map-20260828.md F1）。
    """
    p = Path(str(path))
    name = p.name.casefold()
    if name == ".env" or name.startswith(".env."):
        return ".env 系文件"
    if name in _SENSITIVE_NAMES:
        return f"已知凭据文件名 {p.name}"
    if name.endswith(_SENSITIVE_SUFFIXES):
        return f"密钥类后缀 {p.suffix}"
    parts = {part.casefold() for part in p.parts}
    hit = parts & _SENSITIVE_DIRS
    if hit:
        return f"敏感目录 {sorted(hit)[0]}"
    return None


def redact_secret_lines(text):
    """把明文 secret 赋值的值部遮掉（只作用于送往模型的副本）。"""
    return _SECRET_LINE.sub(
        lambda m: m.group(1) + m.group(2) + "[REDACTED]", str(text or ""))


class AttachmentError(RuntimeError):
    """An explicit attachment cannot be represented safely."""


def _under(path, parent):
    child = Path(path).resolve(strict=False)
    root = Path(parent).resolve(strict=False)
    return child == root or root in child.parents


def _private(path, mode):
    try:
        target = Path(path)
        if not target.is_symlink():
            os.chmod(target, mode)
    except OSError:
        pass


def _ensure_root(root):
    root = Path(root)
    if paths.under_protected(root):
        raise AttachmentError("attachment snapshot 不能写入受保护路径")
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if root.is_symlink():
        raise AttachmentError(f"attachment root 不能是符号链接：{root}")
    _private(root, 0o700)
    return root


def _image_mime(data):
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png", ".png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg", ".jpg"
    if data.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif", ".gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp", ".webp"
    return None, None


def _atomic_snapshot(root, digest, suffix, data):
    root = _ensure_root(root)
    target = root / f"{digest}{suffix}"
    if target.exists():
        if target.is_symlink() or not target.is_file():
            raise AttachmentError(f"attachment snapshot 目标不安全：{target}")
        existing = target.read_bytes()
        if hashlib.sha256(existing).hexdigest() != digest:
            raise AttachmentError(f"attachment snapshot 哈希冲突：{target}")
        _private(target, 0o600)
        return target
    fd, temp_name = tempfile.mkstemp(prefix=".attachment-", dir=root)
    temp = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        _private(temp, 0o600)
        os.replace(temp, target)
        _private(target, 0o600)
    finally:
        try:
            if temp.exists():
                temp.unlink()
        except OSError:
            pass
    return target


def references(text):
    """Return explicit ``@path`` references without treating emails as paths."""
    value = str(text or "")
    out = []
    for match in _TOKEN.finditer(value):
        path = next(group for group in match.groups() if group is not None)
        out.append({"token": match.group(0), "path": path,
                    "start": match.start(), "end": match.end()})
    return out


def _resolve(source, cwd):
    raw = Path(os.path.expanduser(str(source)))
    candidate = raw if raw.is_absolute() else Path(cwd) / raw
    absolute = candidate.absolute()
    if paths.under_protected(absolute):
        raise AttachmentError(
            f"@ 引用拒绝直接读取只读归档：{absolute}；"
            f"请先把所需文件复制到工作目录（{cwd}）")
    if not absolute.exists():
        raise AttachmentError(f"@ 引用不存在：{source}")
    if absolute.is_symlink():
        raise AttachmentError(f"@ 引用不能是符号链接：{source}")
    resolved = absolute.resolve(strict=True)
    if paths.under_protected(resolved):
        raise AttachmentError(
            f"@ 引用拒绝直接读取只读归档：{resolved}；"
            f"请先把所需文件复制到工作目录（{cwd}）")
    if not resolved.is_file():
        raise AttachmentError(f"@ 引用必须是普通文件：{resolved}")
    reason = is_sensitive_path(resolved)
    if reason:
        raise AttachmentError(
            f"@ 引用拒绝直接发送潜在凭据文件（{reason}）：{resolved}")
    return resolved


def _project_text(value):
    if len(value) <= MAX_TEXT_CHARS:
        return value, False, 0
    head_chars = MAX_TEXT_CHARS * 7 // 10
    tail_chars = MAX_TEXT_CHARS - head_chars
    omitted = len(value) - head_chars - tail_chars
    marker = (
        "\n\n[zylab @file 显式截断："
        f"原文 {len(value):,} 字符，省略中间 {omitted:,} 字符；"
        "如需完整证据请用 read_file 分段读取]\n\n")
    return value[:head_chars] + marker + value[-tail_chars:], True, omitted


def prepare_user_message(text, *, cwd=None, supports_image=None, root=None):
    """Snapshot explicit references and return one canonical user message."""
    raw = str(text or "")
    found = references(raw)
    if not found:
        return {"role": "user", "content": raw}
    if len(found) > MAX_ATTACHMENTS:
        raise AttachmentError(
            f"单条输入最多 {MAX_ATTACHMENTS} 个 @ 引用（当前 {len(found)}）")
    selected_root = Path(root or ROOT)
    cwd = os.path.abspath(str(cwd or os.getcwd()))
    manifest = []
    total_text_chars = 0
    seen = set()
    for ref in found:
        source = _resolve(ref["path"], cwd)
        size = source.stat().st_size
        if size > max(MAX_TEXT_SOURCE_BYTES, MAX_IMAGE_BYTES):
            raise AttachmentError(
                f"@ 引用过大：{source}（{size:,} bytes）")
        data = source.read_bytes()
        # stat/read 之间文件可能被替换或增长；以真正要 snapshot/发送的
        # 字节数再做一次上限判断，不能让 TOCTOU 绕过内存与请求预算。
        size = len(data)
        if size > max(MAX_TEXT_SOURCE_BYTES, MAX_IMAGE_BYTES):
            raise AttachmentError(
                f"@ 引用读取后超过上限：{source}（{size:,} bytes）")
        digest = hashlib.sha256(data).hexdigest()
        mime, suffix = _image_mime(data)
        kind = "image" if mime else "text"
        identity = (kind, digest)
        if identity in seen:
            continue
        seen.add(identity)
        if kind == "image":
            if size > MAX_IMAGE_BYTES:
                raise AttachmentError(
                    f"图片超过 {MAX_IMAGE_BYTES // (1024 * 1024)} MiB：{source}")
            if supports_image is not True:
                raise AttachmentError(
                    "当前模型没有已确认的图片输入能力；请用 /model 切换到图片模型，"
                    "或先用 /probe 验证 supports_image")
        else:
            if size > MAX_TEXT_SOURCE_BYTES:
                raise AttachmentError(
                    f"文本超过 {MAX_TEXT_SOURCE_BYTES:,} bytes：{source}；"
                    "请拆分后引用")
            try:
                decoded = data.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise AttachmentError(
                    f"非 UTF-8/不支持的二进制文件：{source}") from exc
            if "\x00" in decoded:
                raise AttachmentError(f"文本含 NUL，按二进制拒绝：{source}")
            projected, truncated, omitted = _project_text(decoded)
            total_text_chars += len(projected)
            if total_text_chars > MAX_TOTAL_TEXT_CHARS:
                raise AttachmentError(
                    f"单条输入的文本附件投影合计超过 "
                    f"{MAX_TOTAL_TEXT_CHARS:,} 字符；请拆分到多轮")
            mime, suffix = "text/plain; charset=utf-8", ".txt"
        snapshot = _atomic_snapshot(selected_root, digest, suffix, data)
        manifest.append({
            "version": 1,
            "kind": kind,
            "name": source.name,
            "source_path": str(source),
            "snapshot_path": str(snapshot),
            "sha256": digest,
            "bytes": len(data),
            "mime": mime,
        })
        if kind == "text":
            manifest[-1].update({
                "chars": len(decoded),
                "projected_chars": len(projected),
                "truncated": truncated,
                "omitted_chars": omitted,
            })
    message = {"role": "user", "content": raw,
               "_kc_attachments": manifest}
    return message


def _snapshot_bytes(item):
    path = Path(str(item.get("snapshot_path") or ""))
    if not path.is_absolute() or not _under(path, ROOT):
        raise AttachmentError(f"attachment snapshot 越界：{path}")
    if path.is_symlink() or not path.is_file():
        raise AttachmentError(f"attachment snapshot 丢失或不安全：{path}")
    data = path.read_bytes()
    expected = str(item.get("sha256") or "")
    actual = hashlib.sha256(data).hexdigest()
    if not expected or actual != expected:
        raise AttachmentError(
            f"attachment snapshot 哈希不匹配：{path}（expected {expected[:12]}）")
    return data


def _expanded_text(message):
    text = str(message.get("content") or "")
    for item in message.get("_kc_attachments") or []:
        if item.get("kind") != "text":
            continue
        data = _snapshot_bytes(item)
        body, _, _ = _project_text(data.decode("utf-8"))
        text += (
            "\n\n<file-context "
            f"path={item.get('source_path')!r} "
            f"sha256={item.get('sha256')!r} bytes={len(data)}>\n"
            f"{body}\n</file-context>")
    return text


def materialize_provider_messages(messages, *, embed_images=True):
    """Deep-copy canonical messages into OpenAI-compatible provider input."""
    out = []
    for original in messages or []:
        message = copy.deepcopy(original)
        manifest = list(message.get("_kc_attachments") or [])
        for field in _INTERNAL_FIELDS:
            message.pop(field, None)
        if message.get("role") != "user" or not manifest:
            out.append(message)
            continue
        text = _expanded_text(original)
        images = [item for item in manifest if item.get("kind") == "image"]
        if not images:
            message["content"] = text
            out.append(message)
            continue
        content = [{"type": "text", "text": text}]
        for item in images:
            if embed_images:
                encoded = base64.b64encode(_snapshot_bytes(item)).decode("ascii")
                url = f"data:{item['mime']};base64,{encoded}"
            else:
                url = (
                    f"data:{item.get('mime')};base64,"
                    f"<sha256:{item.get('sha256')} bytes:{item.get('bytes')}>")
            content.append({"type": "image_url", "image_url": {"url": url}})
        message["content"] = content
        out.append(message)
    return out


def stats(messages):
    items = [item for message in messages or []
             for item in (message.get("_kc_attachments") or [])]
    return {
        "count": len(items),
        "text_count": sum(item.get("kind") == "text" for item in items),
        "image_count": sum(item.get("kind") == "image" for item in items),
        "bytes": sum(max(0, int(item.get("bytes") or 0)) for item in items),
        "image_token_estimate": (
            sum(item.get("kind") == "image" for item in items)
            * IMAGE_TOKEN_ESTIMATE),
        "image_token_estimator": IMAGE_TOKEN_ESTIMATOR,
    }


def strip_internal(message):
    """Keep user text verbatim while removing ephemeral expansion metadata."""
    value = copy.deepcopy(message)
    for field in _INTERNAL_FIELDS:
        value.pop(field, None)
    return value
