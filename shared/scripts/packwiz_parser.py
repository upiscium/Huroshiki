#!/usr/bin/env python3
from __future__ import annotations

from dataclasses import dataclass
import codecs
import re
from typing import Iterable


@dataclass(frozen=True)
class MenuItem:
    """One numbered option printed by Packwiz/wmenu."""

    index: int
    label: str
    is_default: bool = False
    is_cancel: bool = False


@dataclass(frozen=True)
class ParserEvent:
    kind: str
    message: str = ""
    items: tuple[MenuItem, ...] = ()


_MENU_ITEM_RE = re.compile(r"^\s*(\d+)\s*[\)\].:-]\s*(.*?)\s*$")
_MENU_PROMPT_RE = re.compile(r"choose\s+(?:a\s+)?(?:number|option)", re.IGNORECASE)
_YES_NO_RE = re.compile(
    r"(?:\([Yy]/[Nn]\)|\([Nn]/[Yy]\)|\[[Yy]/[Nn]\]|\[[Nn]/[Yy]\]|\b[Yy]/[Nn]\b|\b[Nn]/[Yy]\b)"
)
_SEARCH_RE = re.compile(r"^Searching\s+(Modrinth|CurseForge)\.\.\.$", re.IGNORECASE)
_ERROR_RE = re.compile(
    r"(?:failed|error|no projects found|cancelled|canceled)",
    re.IGNORECASE,
)


class TerminalNormalizer:
    """Small terminal-stream normalizer for Packwiz's line-oriented output.

    It interprets carriage returns, backspaces and the ANSI control sequences
    Packwiz commonly emits for colours and progress lines. It is deliberately
    not a complete terminal emulator; unknown control sequences are ignored.
    """

    def __init__(self) -> None:
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self._line: list[str] = []
        self._cursor = 0
        self._state = "normal"
        self._csi = ""

    @property
    def current_line(self) -> str:
        return "".join(self._line).rstrip("\x00")

    def feed(self, data: bytes, *, final: bool = False) -> list[str]:
        text = self._decoder.decode(data, final=final)
        completed: list[str] = []

        for char in text:
            if self._state == "osc":
                if char == "\x07":
                    self._state = "normal"
                elif char == "\x1b":
                    self._state = "osc_escape"
                continue

            if self._state == "osc_escape":
                self._state = "normal" if char == "\\" else "osc"
                continue

            if self._state == "escape":
                if char == "[":
                    self._state = "csi"
                    self._csi = ""
                elif char == "]":
                    self._state = "osc"
                else:
                    self._state = "normal"
                continue

            if self._state == "csi":
                self._csi += char
                if "@" <= char <= "~":
                    self._apply_csi(self._csi)
                    self._state = "normal"
                    self._csi = ""
                continue

            if char == "\x1b":
                self._state = "escape"
            elif char == "\r":
                self._cursor = 0
            elif char == "\n":
                completed.append(self.current_line.rstrip())
                self._line.clear()
                self._cursor = 0
            elif char == "\b":
                self._cursor = max(0, self._cursor - 1)
            elif char == "\t":
                spaces = 8 - (self._cursor % 8)
                for _ in range(spaces):
                    self._put(" ")
            elif char >= " " and char != "\x7f":
                self._put(char)

        return completed

    def _put(self, char: str) -> None:
        if self._cursor < len(self._line):
            self._line[self._cursor] = char
        else:
            if self._cursor > len(self._line):
                self._line.extend(" " for _ in range(self._cursor - len(self._line)))
            self._line.append(char)
        self._cursor += 1

    def _apply_csi(self, sequence: str) -> None:
        final = sequence[-1]
        params = sequence[:-1]
        values: list[int] = []
        for value in params.lstrip("?").split(";"):
            match = re.match(r"\d+", value)
            values.append(int(match.group(0)) if match else 0)
        amount = values[0] if values and values[0] else 1

        if final == "K":
            mode = values[0] if values else 0
            if mode == 0:
                del self._line[self._cursor :]
            elif mode == 1:
                for index in range(min(self._cursor + 1, len(self._line))):
                    self._line[index] = " "
            elif mode == 2:
                self._line.clear()
                self._cursor = 0
        elif final == "G":
            self._cursor = max(0, amount - 1)
        elif final == "C":
            self._cursor += amount
        elif final == "D":
            self._cursor = max(0, self._cursor - amount)
        # SGR (m), cursor visibility, colour and unsupported controls are ignored.


class PackwizOutputParser:
    """Convert Packwiz PTY bytes into stable, high-level UI events."""

    def __init__(self) -> None:
        self.terminal = TerminalNormalizer()
        self._menu_items: dict[int, MenuItem] = {}
        self._last_prompt_signature: tuple[tuple[int, str], ...] | None = None
        self._menu_prompt_active = False
        self._last_current_line = ""
        self._last_confirmation_signature: str | None = None
        self.normalized_lines: list[str] = []

    def feed(self, data: bytes, *, final: bool = False) -> list[ParserEvent]:
        events: list[ParserEvent] = []
        completed = self.terminal.feed(data, final=final)
        for line in completed:
            events.extend(self._consume_line(line))

        current = self.terminal.current_line.rstrip()
        if current != self._last_current_line:
            self._last_current_line = current
            events.extend(self._consume_prompt(current))

        if final and current:
            events.extend(self._consume_line(current))
            self._last_current_line = ""

        return events

    def _consume_line(self, line: str) -> list[ParserEvent]:
        clean = line.rstrip()
        if clean:
            self.normalized_lines.append(clean)
        events: list[ParserEvent] = []

        item = self._parse_menu_item(clean)
        if item is not None:
            if self._menu_prompt_active:
                self._menu_items.clear()
                self._last_prompt_signature = None
                self._menu_prompt_active = False
            self._menu_items[item.index] = item
            return events

        if clean and self._menu_prompt_active and not _MENU_PROMPT_RE.search(clean):
            self._menu_items.clear()
            self._last_prompt_signature = None
            self._menu_prompt_active = False

        if clean and _YES_NO_RE.search(clean) is None:
            self._last_confirmation_signature = None

        search = _SEARCH_RE.match(clean)
        if search:
            self._menu_items.clear()
            self._last_prompt_signature = None
            self._last_confirmation_signature = None
            events.append(ParserEvent("search_started", search.group(1)))

        events.extend(self._consume_prompt(clean))

        if clean and _ERROR_RE.search(clean):
            events.append(ParserEvent("diagnostic", clean))
        elif clean:
            events.append(ParserEvent("output", clean))
        return events

    def _consume_prompt(self, line: str) -> list[ParserEvent]:
        if not line:
            return []

        if _MENU_PROMPT_RE.search(line) and self._menu_items:
            items = tuple(self._menu_items[index] for index in sorted(self._menu_items))
            signature = tuple((item.index, item.label) for item in items)
            if signature != self._last_prompt_signature:
                self._last_prompt_signature = signature
                self._menu_prompt_active = True
                return [ParserEvent("search_results", line, items)]
            return []

        yes_no = _YES_NO_RE.search(line)
        if yes_no is not None:
            signature = line[: yes_no.end()].strip()
            if signature != self._last_confirmation_signature:
                self._last_confirmation_signature = signature
                return [ParserEvent("confirmation", line)]
            return []

        return []

    @staticmethod
    def _parse_menu_item(line: str) -> MenuItem | None:
        match = _MENU_ITEM_RE.match(line)
        if match is None:
            return None
        index = int(match.group(1))
        label = match.group(2).strip()
        is_default = label.startswith("*")
        label = label.lstrip("*").strip()
        if not label:
            return None
        is_cancel = label.casefold() in {"cancel", "quit", "exit"}
        return MenuItem(
            index=index,
            label=label,
            is_default=is_default,
            is_cancel=is_cancel,
        )


def visible_menu_items(items: Iterable[MenuItem]) -> tuple[MenuItem, ...]:
    return tuple(item for item in items if not item.is_cancel)
