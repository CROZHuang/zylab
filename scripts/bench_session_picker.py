#!/usr/bin/env python3
"""Measure the real ``/resume`` 10k-session first paint through a PTY.

Each trial starts a fresh Python process and real zylab REPL, then writes
``/resume`` to its PTY. The child takes the production path through the
InputPump reader, command dispatch, ``open_sessions_panel``, ``Session.pick``
and TerminalRenderer. A benchmark-only footer sentinel is observed on the PTY
master after all visible option rows have been rendered.

The index lives on ``--temp-root`` and is shared by all children. Warmup
trials therefore prime the PVC page cache; measured trials still pay for a
fresh process, JSON parse, scope/filter/sort, picker construction and paint.
No network request is allowed. Representative canonical transcripts are
present and every transcript read is counted, so an eager-read regression
cannot be reported as zero.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import math
import os
import pty
import select
import statistics
import struct
import subprocess
import sys
import tempfile
import termios
import time
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from core import store  # noqa: E402


FRAME_SENTINEL = "ZYLAB_BENCH_PICKER_FRAME_7B9C"
PANEL_CLOSED_SENTINEL = "ZYLAB_BENCH_PICKER_CLOSED_4D2A"
CHILD_RESULT_PREFIX = b"ZYLAB_BENCH_PICKER_RESULT:"


CHILD = r'''
import json
import os
import time
from pathlib import Path

import zylab
from core import client, store, tui

frame_sentinel = os.environ["ZYLAB_BENCH_FRAME_SENTINEL"]
panel_closed_sentinel = os.environ["ZYLAB_BENCH_CLOSED_SENTINEL"]
store.ensure_home()

transcript_reads = {"n": 0}
network_calls = {"n": 0}
original_read_text = Path.read_text


def tracked_read_text(path, *args, **kwargs):
    candidate = Path(path)
    if candidate.parent == store.SESSIONS and candidate.suffix == ".json":
        transcript_reads["n"] += 1
    return original_read_text(candidate, *args, **kwargs)


Path.read_text = tracked_read_text


class TrackingPump(tui.InputPump):
    picker_rows = []

    def open_picker(self, rows, **kwargs):
        title = str(kwargs.get("title") or "")
        if title.startswith("Resume session"):
            type(self).picker_rows.append(len(rows))
            kwargs = dict(kwargs)
            controls = str(kwargs.get("controls") or "")
            # Controls render after every visible option row. Put the marker
            # first so narrow clipping cannot remove the timing endpoint.
            kwargs["controls"] = f"{frame_sentinel} · {controls}"
        return super().open_picker(rows, **kwargs)


tui.InputPump = TrackingPump
panel_calls = {"n": 0}
original_panel = zylab.open_sessions_panel


def tracked_panel(session):
    panel_calls["n"] += 1
    result = original_panel(session)
    if result is None and session.renderer is not None:
        session.renderer.write_output(
            f"{panel_closed_sentinel}\r\n", source="status")
    return result


zylab.open_sessions_panel = tracked_panel


def no_network(*_args, **_kwargs):
    network_calls["n"] += 1
    raise AssertionError("picker benchmark attempted network I/O")


client.stream_chat = no_network


class FakeAgent:
    def __init__(self):
        self.session_id = "picker-bench-current"
        self.model = "picker-bench-model"
        self.gateway = client.GATEWAY
        self.messages = [{"role": "system", "content": "benchmark"}]
        self.tokens_in = 0
        self.tokens_out = 0
        self.last_total = 0
        self.turns = 0
        self.cache_read = 0
        self.cache_write = 0
        self.cache_reported = False
        self.compact_failed = None
        self.ctx_limit = 100_000
        self.compact_at = 70_000
        self.ctx_known = True
        self.ctx_limit_source = "benchmark"
        self.context_summary = None
        self.context_invalid_reason = None
        self._compact_failed_key = None
        self._last_age_notice_key = None
        self._seen_ok_hi = 0
        self.started = time.time()

    def context_snapshot(self):
        return {"summary": self.context_summary}

    def load_context(self, context):
        self.context_summary = (context or {}).get("summary")

    def set_model(self, model):
        self.model = model


agent = FakeAgent()
session = zylab.Session(agent)
agent.confirm = session.confirm
zylab.repl(session, agent.model)
print(
    "ZYLAB_BENCH_PICKER_RESULT:"
    + json.dumps({
        "picker_rows": TrackingPump.picker_rows,
        "panel_calls": panel_calls["n"],
        "transcript_reads": transcript_reads["n"],
        "network_calls": network_calls["n"],
    }, ensure_ascii=False)
)
'''


def nearest_rank(values: list[float], q: float) -> float:
    ordered = sorted(values)
    if not ordered:
        raise ValueError("percentile requires at least one sample")
    return ordered[max(0, math.ceil(q * len(ordered)) - 1)]


def build_index(count: int, cwd: str) -> dict:
    now = "2026-08-25T12:00:00+00:00"
    sessions = {}
    for index in range(count):
        session_id = f"{index:012x}"
        sessions[session_id] = {
            "id": session_id,
            "title": f"session-{index:05d}-context-router",
            "status": "active",
            "model": "kimi-k3-256k",
            "gateway": "deepinfer",
            "cwd": cwd,
            "repo_root": cwd,
            "git_branch": "master",
            "created": now,
            "updated": f"2026-08-25T12:{index % 60:02d}:00+00:00",
            "count": index % 80,
            "last_context_tokens": index * 10,
            "parent_session_id": "",
            "fork_seq": None,
        }
    return {
        "version": store.SESSION_INDEX_VERSION,
        "updated": now,
        "sessions": sessions,
        "errors": [],
    }


def read_more(fd: int, buffer: bytearray, timeout: float) -> None:
    ready, _, _ = select.select([fd], [], [], max(0.0, timeout))
    if not ready:
        return
    try:
        chunk = os.read(fd, 65536)
    except OSError:
        return
    if chunk:
        buffer.extend(chunk)


def consume_until(fd: int, buffer: bytearray, needle: bytes,
                  timeout: float) -> tuple[int, int]:
    deadline = time.perf_counter() + timeout
    while True:
        found = buffer.find(needle)
        if found >= 0:
            consumed = found + len(needle)
            del buffer[:consumed]
            return time.perf_counter_ns(), consumed
        remaining = deadline - time.perf_counter()
        if remaining <= 0:
            raise TimeoutError(
                f"waiting {needle!r}; tail={bytes(buffer[-1000:])!r}")
        read_more(fd, buffer, min(0.02, remaining))


def consume_line(fd: int, buffer: bytearray, timeout: float) -> bytes:
    deadline = time.perf_counter() + timeout
    while True:
        for ending in (b"\r\n", b"\n"):
            found = buffer.find(ending)
            if found >= 0:
                line = bytes(buffer[:found])
                del buffer[:found + len(ending)]
                return line
        remaining = deadline - time.perf_counter()
        if remaining <= 0:
            raise TimeoutError(
                f"waiting child result line; tail={bytes(buffer[-1000:])!r}")
        read_more(fd, buffer, min(0.02, remaining))


def drain_until_quiet(fd: int, buffer: bytearray, *, quiet: float = 0.03,
                      timeout: float = 0.5) -> None:
    deadline = time.perf_counter() + timeout
    quiet_deadline = time.perf_counter() + quiet
    while time.perf_counter() < deadline:
        remaining = min(deadline, quiet_deadline) - time.perf_counter()
        if remaining <= 0:
            return
        ready, _, _ = select.select([fd], [], [], remaining)
        if not ready:
            return
        before = len(buffer)
        read_more(fd, buffer, 0.0)
        if len(buffer) > before:
            quiet_deadline = time.perf_counter() + quiet


def run_trial(args: argparse.Namespace, trial_number: int,
              bench_home: Path) -> dict:
    master, slave = pty.openpty()
    fcntl.ioctl(
        slave,
        termios.TIOCSWINSZ,
        struct.pack("HHHH", args.rows, args.cols, 0, 0),
    )
    env = os.environ.copy()
    env["HOME"] = str(bench_home)
    env["ZYLAB_BENCH_FRAME_SENTINEL"] = FRAME_SENTINEL
    env["ZYLAB_BENCH_CLOSED_SENTINEL"] = PANEL_CLOSED_SENTINEL
    proc = subprocess.Popen(
        [sys.executable, "-c", CHILD],
        stdin=slave,
        stdout=slave,
        stderr=slave,
        cwd=REPO_ROOT,
        env=env,
        close_fds=True,
    )
    os.close(slave)
    buffer = bytearray()
    try:
        consume_until(master, buffer, "空闲".encode("utf-8"), 8.0)
        drain_until_quiet(master, buffer)
        buffer.clear()

        started = time.perf_counter_ns()
        os.write(master, b"/resume\r")
        painted, rendered_bytes = consume_until(
            master, buffer, FRAME_SENTINEL.encode("ascii"), 8.0)
        elapsed_ms = (painted - started) / 1_000_000

        # Finish the actual command cleanly so the child can report the path
        # counters that form part of the acceptance result.
        drain_until_quiet(master, buffer)
        buffer.clear()
        os.write(master, b"\x1b")
        consume_until(
            master, buffer, PANEL_CLOSED_SENTINEL.encode("ascii"), 4.0)
        drain_until_quiet(master, buffer)
        buffer.clear()
        os.write(master, b"/exit\r")
        consume_until(master, buffer, CHILD_RESULT_PREFIX, 4.0)
        child_line = consume_line(master, buffer, 1.0)
        child_result = json.loads(child_line.decode("utf-8"))
        proc.wait(timeout=3.0)
        if proc.returncode != 0:
            raise RuntimeError(
                f"picker benchmark child {trial_number} exited "
                f"{proc.returncode}")
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=3.0)
        os.close(master)

    picker_rows = child_result.get("picker_rows") or []
    return {
        "trial": trial_number,
        "elapsed_ms": elapsed_ms,
        "rows_seen": picker_rows[0] if len(picker_rows) == 1 else None,
        "picker_opens": len(picker_rows),
        "rendered_bytes": rendered_bytes,
        "panel_calls": child_result.get("panel_calls"),
        "transcript_reads": child_result.get("transcript_reads"),
        "network_calls": child_result.get("network_calls"),
    }


def summarize(args: argparse.Namespace, trials: list[dict]) -> dict:
    values = [float(trial["elapsed_ms"]) for trial in trials]
    rows_exact = all(
        trial.get("rows_seen") == args.sessions for trial in trials)
    painted = all(
        int(trial.get("rendered_bytes") or 0) > 0 for trial in trials)
    one_picker = all(
        trial.get("picker_opens") == 1
        and trial.get("panel_calls") == 1
        for trial in trials)
    transcript_clean = all(
        trial.get("transcript_reads") == 0 for trial in trials)
    network_clean = all(
        trial.get("network_calls") == 0 for trial in trials)
    p95 = nearest_rank(values, 0.95)
    invariants = {
        "rows_exact": rows_exact,
        "painted_bytes_positive": painted,
        "one_real_panel_per_trial": one_picker,
        "zero_transcript_reads": transcript_clean,
        "zero_network_calls": network_clean,
    }
    passed = p95 < args.threshold_ms and all(invariants.values())
    return {
        "method": (
            "fresh subprocess real REPL -> PTY /resume -> InputPump reader -> "
            "open_sessions_panel -> Session.pick -> TerminalRenderer footer"
        ),
        "cache_semantics": (
            "one shared PVC index; warmup primes page cache; every measured "
            "trial starts a fresh Python/REPL process"
        ),
        "temp_root": str(args.temp_root),
        "sessions": args.sessions,
        "trials": args.trials,
        "warmup": args.warmup,
        "pty": {"rows": args.rows, "cols": args.cols},
        "p50_ms": round(statistics.median(values), 3),
        "p95_ms": round(p95, 3),
        "max_ms": round(max(values), 3),
        "rows_seen": list(dict.fromkeys(
            trial.get("rows_seen") for trial in trials)),
        "rendered_bytes_min": min(
            int(trial.get("rendered_bytes") or 0) for trial in trials),
        "transcript_reads": sum(
            int(trial.get("transcript_reads") or 0) for trial in trials),
        "network_calls": sum(
            int(trial.get("network_calls") or 0) for trial in trials),
        "invariants": invariants,
        "threshold_p95_ms": args.threshold_ms,
        "trial_results": [
            {
                key: round(value, 3) if isinstance(value, float) else value
                for key, value in trial.items()
            }
            for trial in trials
        ],
        "pass": passed,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sessions", type=int, default=10_000)
    parser.add_argument("--trials", type=int, default=15)
    parser.add_argument("--warmup", type=int, default=2)
    parser.add_argument("--threshold-ms", type=float, default=200.0)
    parser.add_argument(
        "--temp-root", type=Path, default=Path.home())
    parser.add_argument("--rows", type=int, default=40)
    parser.add_argument("--cols", type=int, default=140)
    args = parser.parse_args()
    if (args.sessions < 1 or args.trials < 1 or args.warmup < 0
            or args.threshold_ms <= 0 or args.rows < 5 or args.cols < 20):
        parser.error(
            "sessions/trials/threshold must be positive; warmup non-negative; "
            "PTY must be at least 5x20")
    args.temp_root = args.temp_root.expanduser().resolve()
    if not args.temp_root.is_dir():
        parser.error(f"temp root does not exist: {args.temp_root}")
    return args


def main() -> int:
    args = parse_args()
    with tempfile.TemporaryDirectory(
            prefix="zylab-picker-", dir=args.temp_root) as tmp:
        home = Path(tmp)
        state_root = home / ".zylab"
        state_root.mkdir(mode=0o700)
        sessions = state_root / "sessions"
        sessions.mkdir(mode=0o700)
        index = build_index(args.sessions, str(REPO_ROOT))
        index_path = state_root / "session-index.json"
        index_path.write_text(
            json.dumps(index, ensure_ascii=False, separators=(",", ":")),
            encoding="utf-8",
        )
        os.chmod(index_path, 0o600)

        # Put canonical files behind the rows that can appear on the first
        # page. They are setup-only and make an eager preview/scan observable.
        visible = sorted(
            index["sessions"].values(),
            key=lambda row: row["updated"],
            reverse=True,
        )[:12]
        for row in visible:
            record = dict(row)
            record["messages"] = [
                {"role": "system", "content": "transcript sentinel"},
                {"role": "user", "content": row["title"]},
            ]
            record["context"] = None
            path = sessions / f"{row['id']}.json"
            path.write_text(
                json.dumps(
                    record, ensure_ascii=False, separators=(",", ":")),
                encoding="utf-8",
            )
            os.chmod(path, 0o600)

        all_trials = [
            run_trial(args, number, home)
            for number in range(1, args.warmup + args.trials + 1)
        ]
        measured = all_trials[args.warmup:]

    result = summarize(args, measured)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
