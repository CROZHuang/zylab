"""Small dependency-free terminal screen model for Gate 0 PTY tests.

The production TUI intentionally writes ANSI incrementally.  A raw output
string therefore contains superseded frames and is not a reliable assertion
of what a user sees.  This module models the tiny subset of terminal state
used by ``core.tui.TerminalRenderer`` and exposes normalized cell grids.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
import unicodedata

from core import tui


@dataclass(frozen=True)
class Cell:
    """One normalized terminal cell.

    ``text`` contains the grapheme cluster at its leading cell.  A wide
    glyph's following cell is represented by ``continuation=True`` and has
    empty text.  Styles are deliberately omitted: Gate 0 compares layout and
    logical text, not palette escape sequences.
    """

    text: str = " "
    continuation: bool = False

    @property
    def normalized(self):
        return "" if self.continuation else self.text


class CellGrid:
    """Rectangular, row-addressable terminal cell grid."""

    def __init__(self, cols: int, rows: int):
        self.cols = max(1, int(cols))
        self.rows = max(1, int(rows))
        self.cells = [
            [Cell() for _ in range(self.cols)] for _ in range(self.rows)
        ]

    def __getitem__(self, row):
        return self.cells[row]

    def __len__(self):
        return self.rows

    def clone(self):
        result = CellGrid(self.cols, self.rows)
        result.cells = [list(row) for row in self.cells]
        return result

    def row_text(self, row, *, trim=False):
        value = "".join(cell.normalized for cell in self.cells[int(row)])
        return value.rstrip() if trim else value

    def text_rows(self, *, trim=False):
        return [self.row_text(row, trim=trim) for row in range(self.rows)]

    def normalized(self):
        return tuple(tuple(cell.normalized for cell in row) for row in self.cells)

    def __eq__(self, other):
        return isinstance(other, CellGrid) and (
            self.cols, self.rows, self.normalized()
        ) == (other.cols, other.rows, other.normalized())

    def __repr__(self):  # pragma: no cover - only useful on assertion failure
        return f"CellGrid({self.cols}x{self.rows}, {self.text_rows(trim=True)!r})"


_CSI_FINAL = re.compile(r"[\x40-\x7e]")


class _Screen:
    def __init__(self, cols, rows):
        self.cols = max(1, int(cols))
        self.rows = max(1, int(rows))
        self.primary = CellGrid(self.cols, self.rows)
        self.alternate = CellGrid(self.cols, self.rows)
        self.active = "primary"
        self.row = 0
        self.col = 0
        self._saved_primary_cursor = (0, 0)
        self.mouse_modes = set()
        self._after_zwj = False
        self._regional_pending = False

    @property
    def grid(self):
        return self.primary if self.active == "primary" else self.alternate

    def _clear(self, grid=None):
        grid = grid or self.grid
        grid.cells = [
            [Cell() for _ in range(grid.cols)] for _ in range(grid.rows)
        ]

    def _scroll(self):
        grid = self.grid
        grid.cells.pop(0)
        grid.cells.append([Cell() for _ in range(grid.cols)])
        self.row = grid.rows - 1

    def _linefeed(self):
        self.row += 1
        if self.row >= self.rows:
            self._scroll()

    def _put(self, char):
        grid = self.grid
        code = ord(char)
        combining = (
            unicodedata.combining(char)
            or char == "\u200d"
            or 0xFE00 <= code <= 0xFE0F
            or 0x1F3FB <= code <= 0x1F3FF
            or unicodedata.category(char) == "Cf"
        )
        if combining and self.col > 0:
            previous_col = self.col - 1
            while (previous_col > 0
                   and grid[self.row][previous_col].continuation):
                previous_col -= 1
            previous = grid[self.row][previous_col]
            if not previous.continuation:
                grid[self.row][previous_col] = Cell(previous.text + char)
            self._after_zwj = char == "\u200d"
            return
        if self._after_zwj and self.col > 0:
            previous_col = self.col - 1
            while (previous_col > 0
                   and grid[self.row][previous_col].continuation):
                previous_col -= 1
            previous = grid[self.row][previous_col]
            if not previous.continuation:
                grid[self.row][previous_col] = Cell(previous.text + char)
                self._after_zwj = False
                return
        self._after_zwj = False

        regional = 0x1F1E6 <= code <= 0x1F1FF
        if regional:
            width = 2 if not self._regional_pending else 0
            self._regional_pending = not self._regional_pending
        else:
            self._regional_pending = False
            width = tui._cell_width(char)
            width = 0 if width is None else width
        if width <= 0:
            return
        if self.col + width > self.cols:
            self._linefeed()
            self.col = 0
        if self.col >= self.cols:
            self._linefeed()
            self.col = 0
        grid[self.row][self.col] = Cell(char)
        if width == 2 and self.col + 1 < self.cols:
            grid[self.row][self.col + 1] = Cell("", continuation=True)
        self.col += width
        if self.col >= self.cols:
            # Terminals defer the actual wrap until the next printable byte;
            # keeping the cursor at the right edge is enough for our model.
            self.col = self.cols

    @staticmethod
    def _params(raw):
        private = raw.startswith("?")
        if private:
            raw = raw[1:]
        values = []
        for part in raw.split(";") if raw else []:
            try:
                values.append(int(part or 0))
            except ValueError:
                values.append(0)
        return private, values

    def _csi(self, raw, final):
        private, values = self._params(raw)
        first = values[0] if values else 0
        if private and first in {1000, 1002, 1006}:
            if final == "h":
                self.mouse_modes.add(first)
            elif final == "l":
                self.mouse_modes.discard(first)
            return
        if private and first == 1049:
            if final == "h":
                self._saved_primary_cursor = (self.row, self.col)
                self.active = "alternate"
                self._clear(self.alternate)
                self.row = self.col = 0
            elif final == "l":
                self.active = "primary"
                self.row, self.col = self._saved_primary_cursor
                self.row = min(self.row, self.rows - 1)
                self.col = min(self.col, self.cols)
            return
        if final in {"H", "f"}:
            self.row = max(0, min(self.rows - 1, (values[0] if values else 1) - 1))
            self.col = max(0, min(self.cols, (values[1] if len(values) > 1 else 1) - 1))
        elif final == "A":
            self.row = max(0, self.row - (first or 1))
        elif final == "B":
            self.row = min(self.rows - 1, self.row + (first or 1))
        elif final in {"C", "a"}:
            self.col = min(self.cols, self.col + (first or 1))
        elif final == "D":
            self.col = max(0, self.col - (first or 1))
        elif final == "G":
            self.col = max(0, min(self.cols, (first or 1) - 1))
        elif final == "d":
            self.row = max(0, min(self.rows - 1, (first or 1) - 1))
        elif final == "J":
            grid = self.grid
            if first == 2 or first == 3:
                self._clear(grid)
            elif first == 0:
                # Erase from the cursor through the end of the display.
                for col in range(self.col, self.cols):
                    grid[self.row][col] = Cell()
                for row in range(self.row + 1, self.rows):
                    grid.cells[row] = [Cell() for _ in range(self.cols)]
            elif first == 1:
                for row in range(0, self.row):
                    grid.cells[row] = [Cell() for _ in range(self.cols)]
                for col in range(0, min(self.col + 1, self.cols)):
                    grid[self.row][col] = Cell()
        elif final == "K":
            grid = self.grid
            if first == 2:
                grid.cells[self.row] = [Cell() for _ in range(self.cols)]
            elif first == 1:
                for col in range(0, min(self.col + 1, self.cols)):
                    grid[self.row][col] = Cell()
            else:
                for col in range(self.col, self.cols):
                    grid[self.row][col] = Cell()

    def feed(self, data):
        if isinstance(data, bytes):
            data = data.decode("utf-8", "replace")
        data = str(data)
        index = 0
        while index < len(data):
            char = data[index]
            if char == "\x1b":
                if index + 1 >= len(data):
                    break
                kind = data[index + 1]
                if kind == "[":
                    match = _CSI_FINAL.search(data, index + 2)
                    if match is None:
                        break
                    self._csi(data[index + 2:match.start()], match.group(0))
                    index = match.end()
                    continue
                if kind == "]":
                    end = index + 2
                    while end < len(data):
                        if data[end] == "\a":
                            end += 1
                            break
                        if data[end] == "\x1b" and end + 1 < len(data) and data[end + 1] == "\\":
                            end += 2
                            break
                        end += 1
                    index = end
                    continue
                # OSC/CSI are the only display-affecting forms used here;
                # skip a two-byte or single-byte ESC sequence otherwise.
                index += 2
                continue
            if char == "\r":
                self.col = 0
            elif char == "\n":
                self._linefeed()
            elif char == "\b":
                self.col = max(0, self.col - 1)
            elif char == "\t":
                self.col = min(self.cols, self.col + (8 - self.col % 8))
            elif ord(char) >= 0x20 and char != "\x7f":
                self._put(char)
            index += 1

    def resize(self, cols, rows):
        cols, rows = max(1, int(cols)), max(1, int(rows))
        for name in ("primary", "alternate"):
            old = getattr(self, name)
            fresh = CellGrid(cols, rows)
            for row in range(min(rows, old.rows)):
                for col in range(min(cols, old.cols)):
                    fresh[row][col] = old[row][col]
            setattr(self, name, fresh)
        self.cols, self.rows = cols, rows
        self.row = min(self.row, rows - 1)
        self.col = min(self.col, cols)

    def result(self):
        return {
            "active_buffer": self.active,
            "primary": self.primary,
            "alternate": self.alternate,
            "cursor": (self.row + 1, self.col + 1),
            "mouse_modes": tuple(sorted(self.mouse_modes)),
        }


class TerminalScreen(_Screen):
    """Public test-only model for incremental feed and explicit resize."""

    pass


def parse_final_screen(data, cols, rows):
    """Parse ANSI output into primary/alternate normalized final screens."""

    model = TerminalScreen(cols, rows)
    model.feed(data)
    return model.result()
