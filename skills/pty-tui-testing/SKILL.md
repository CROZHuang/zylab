---
name: pty-tui-testing
description: Use when testing terminal UI, PTY input, cursor movement, mouse selection, redraws, or synchronization
---
# PTY and TUI testing

Test terminal behavior through a real pseudo-terminal and judge the final screen
state, not only the raw byte stream.

## Harness rules

1. Isolate HOME, zylab state, sessions, artifacts, settings, and cwd in a
   temporary directory. Tests must never write the user's real `~/.zylab/`.
2. Synchronize on dedicated test-only lifecycle markers or inspected state.
   Do not reuse ordinary user-visible footer, prompt, or help text as sentinels.
3. Prefer condition-based readiness over fixed sleeps; bound every wait and
   terminate the child on failure.
4. Exercise the exact input transport: escape sequences, Shift+Enter, bracketed
   paste, mouse mode, resize, cancellation, queueing, or scrollback as relevant.
5. Use a terminal emulator or screen model when redraw, wrapping, cursor
   placement, or erased text matters. Raw output can contain superseded frames.
6. Assert cleanup: child exit, restored terminal modes, stopped threads, closed
   descriptors, and no persistent user-state pollution.

Keep test marker strings out of production UI copy so copy changes cannot create
false readiness. Reproduce reported terminal behavior at the intended width and
height before declaring it fixed.

References:

- AGENTS.md, sections 2, 3, and 5
- tests/pty_harness.py
- tests/test_tui_pty.py
- tests/test_history_and_browse_pty.py
