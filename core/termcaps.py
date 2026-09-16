"""终端能力探测与缓存（DESIGN-TUI-app §2.4：能力探测决定功能，不猜终端）。

启动时在 raw 模式下发几条查询，总预算 ≤ 150 ms；每条都带超时，终端不应答就当不支持。
结果按 (TERM_PROGRAM, TERM_PROGRAM_VERSION, TERM) 缓存到状态目录的 termcaps.json，下次启动
零查询。非 TTY 全关。查询本身只读，不改变终端状态（kitty 协议只问不开）。

`scripts/term_probe.py` 是同一套查询的"人肉版"，用来在新终端上画边界。
"""
import json
import os
import select
import sys
import termios
import time
import tty
from dataclasses import asdict, dataclass

ESC = "\x1b"
BUDGET_SECONDS = 0.15
CACHE_VERSION = 1


@dataclass(frozen=True)
class TermCaps:
    tty: bool = False
    alt_screen: bool = False
    sync_output: bool = False        # DECSET 2026
    truecolor: bool = False
    kitty_keyboard: bool = False     # `CSI ? u` 有应答
    bracketed_paste: bool = False
    sgr_mouse: bool = False
    focus_events: bool = False
    osc52_write: bool = False        # 无法可靠探测写入是否生效；有回读应答时为真，否则按终端身份推断
    background: str = ""             # OSC 11 的 rgb:rrrr/gggg/bbbb
    dark: bool = True
    terminal: str = ""               # XTVERSION 或 DA2
    cols: int = 80
    rows: int = 24

    def as_dict(self):
        return asdict(self)


def _identity(environ=None):
    environ = os.environ if environ is None else environ
    return "|".join(environ.get(k, "") for k in ("TERM_PROGRAM", "TERM_PROGRAM_VERSION", "TERM"))


def _cache_path():
    try:
        from . import paths
        return paths.state_home() / "termcaps.json"
    except Exception:                                   # noqa: BLE001
        return None


def load_cached(identity):
    path = _cache_path()
    if path is None or not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if data.get("version") != CACHE_VERSION or data.get("identity") != identity:
        return None
    try:
        return TermCaps(**data["caps"])
    except (KeyError, TypeError):
        return None


def save_cached(identity, caps):
    path = _cache_path()
    if path is None:
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps({"version": CACHE_VERSION, "identity": identity, "caps": caps.as_dict()},
                                  ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass


class _Query:
    """raw 模式下的短查询；退出时恢复终端属性。"""

    def __init__(self, fd_in, out):
        self.fd = fd_in
        self.out = out
        self.saved = None

    def __enter__(self):
        self.saved = termios.tcgetattr(self.fd)
        tty.setcbreak(self.fd)
        attrs = termios.tcgetattr(self.fd)
        attrs[3] &= ~termios.ECHO
        termios.tcsetattr(self.fd, termios.TCSANOW, attrs)
        return self

    def __exit__(self, *exc):
        if self.saved is not None:
            termios.tcsetattr(self.fd, termios.TCSANOW, self.saved)
        return False

    def ask(self, seq, until, timeout):
        self.out.write(seq)
        self.out.flush()
        buf = b""
        deadline = time.monotonic() + timeout
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                break
            r, _, _ = select.select([self.fd], [], [], left)
            if not r:
                break
            chunk = os.read(self.fd, 512)
            if not chunk:
                break
            buf += chunk
            if until in buf:
                break
        return buf


def _decrqm(q, mode, timeout):
    reply = q.ask(f"{ESC}[?{mode}$p", b"$y", timeout).decode("latin-1")
    if "$y" not in reply:
        return None
    try:
        return int(reply.split(";")[1].split("$")[0])
    except (IndexError, ValueError):
        return None


def _is_dark(rgb):
    """OSC 11 的 rgb:rrrr/gggg/bbbb → 亮度 < 0.5 视为深色。"""
    try:
        parts = rgb.split("rgb:")[1].split("/")
        r, g, b = (int(p[:2], 16) for p in parts[:3])
    except (IndexError, ValueError):
        return True
    return (0.299 * r + 0.587 * g + 0.114 * b) / 255 < 0.5


def probe(*, stream_in=None, stream_out=None, environ=None, budget=BUDGET_SECONDS):
    """真探测。只在 TTY 上；每条查询的超时按预算均分。"""
    environ = os.environ if environ is None else environ
    stream_in = stream_in or sys.stdin
    stream_out = stream_out or sys.stdout
    try:
        is_tty = stream_in.isatty() and stream_out.isatty()
    except (AttributeError, ValueError):
        is_tty = False
    if not is_tty:
        return TermCaps()
    try:
        cols, rows = os.get_terminal_size(stream_out.fileno())
    except OSError:
        cols, rows = 80, 24
    truecolor = environ.get("COLORTERM", "").lower() in ("truecolor", "24bit")
    per = max(0.02, budget / 5)
    fields = {"tty": True, "truecolor": truecolor, "cols": cols, "rows": rows}
    try:
        with _Query(stream_in.fileno(), stream_out) as q:
            xtv = q.ask(f"{ESC}[>0q", b"\\", per).decode("latin-1")
            if "|" in xtv:
                fields["terminal"] = xtv.split("|", 1)[1].rstrip("\x1b\\")
            else:
                da2 = q.ask(f"{ESC}[>c", b"c", per).decode("latin-1")
                fields["terminal"] = da2.strip("\x1b[>c")
            fields["sync_output"] = _decrqm(q, 2026, per) in (1, 2)
            fields["alt_screen"] = _decrqm(q, 1049, per) in (1, 2)
            fields["bracketed_paste"] = _decrqm(q, 2004, per) in (1, 2)
            fields["sgr_mouse"] = _decrqm(q, 1006, per) in (1, 2)
            fields["focus_events"] = _decrqm(q, 1004, per) in (1, 2)
            kitty = q.ask(f"{ESC}[?u", b"u", per)
            fields["kitty_keyboard"] = b"[?" in kitty and kitty.endswith(b"u")
            bg = q.ask(f"{ESC}]11;?\x07", b"\x07", per).decode("latin-1")
            if "rgb:" in bg:
                fields["background"] = "rgb:" + bg.split("rgb:", 1)[1].rstrip("\x07\x1b\\")
                fields["dark"] = _is_dark(fields["background"])
            # OSC 52：只问读；有应答就当读写都放行，没应答按终端身份推断（xterm.js / kitty / wezterm / iTerm 都放行写）
            clip = q.ask(f"{ESC}]52;c;?\x07", b"\x07", per)
            known = any(name in (fields.get("terminal", "") + environ.get("TERM_PROGRAM", "")).lower()
                        for name in ("xterm.js", "vscode", "kitty", "wezterm", "iterm", "alacritty", "foot"))
            fields["osc52_write"] = (b"]52;" in clip) or known
            # 把没读完的应答残渣清掉，免得漏进输入
            q.ask("", b"\x00", 0.02)
    except (OSError, termios.error):
        return TermCaps(tty=True, truecolor=truecolor, cols=cols, rows=rows)
    return TermCaps(**fields)


def detect(*, force=False, environ=None):
    """带缓存的探测：同一终端身份只探一次。"""
    environ = os.environ if environ is None else environ
    identity = _identity(environ)
    if not force:
        cached = load_cached(identity)
        if cached is not None and cached.tty:
            try:
                cols, rows = os.get_terminal_size()
            except OSError:
                cols, rows = cached.cols, cached.rows
            return TermCaps(**{**cached.as_dict(), "cols": cols, "rows": rows})
    caps = probe(environ=environ)
    if caps.tty:
        save_cached(identity, caps)
    return caps
