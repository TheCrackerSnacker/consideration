"""A deliberately small YAML subset parser.

The host configuration is YAML, but pulling in PyYAML for a handful of nested
mappings and sequences is not worth a runtime dependency. This parser supports:

* block mappings and block sequences
* flow sequences and flow mappings (single line)
* single- and double-quoted scalars
* plain scalars: strings, integers, floats, booleans, null
* comments beginning with ``#`` outside quotes

It rejects anything else with a line number. JSON is also valid input, because
JSON is a subset of this parser's supported flow syntax only when it is one
line; callers should use :func:`json.loads` for JSON documents.
"""

from __future__ import annotations

import json
import re
from typing import Any

__all__ = ["YAMLError", "loads"]


class YAMLError(ValueError):
    pass


_INT = re.compile(r"^[+-]?[0-9]+$")
_FLOAT = re.compile(r"^[+-]?(?:[0-9]+\.[0-9]*|\.[0-9]+)(?:[eE][+-]?[0-9]+)?$")


def _strip_comment(line: str) -> str:
    out: list[str] = []
    quote: str | None = None
    i = 0
    while i < len(line):
        ch = line[i]
        if quote:
            out.append(ch)
            if quote == '"' and ch == "\\" and i + 1 < len(line):
                out.append(line[i + 1])
                i += 2
                continue
            if ch == quote:
                if quote == "'" and i + 1 < len(line) and line[i + 1] == "'":
                    out.append("'")
                    i += 2
                    continue
                quote = None
        else:
            if ch in "'\"":
                quote = ch
                out.append(ch)
            elif ch == "#" and (i == 0 or line[i - 1] in " \t"):
                break
            else:
                out.append(ch)
        i += 1
    return "".join(out).rstrip()


def _unquote(text: str, line: int) -> str:
    text = text.strip()
    if len(text) < 2 or text[0] != text[-1]:
        raise YAMLError(f"line {line}: unterminated quoted string")
    if text[0] == '"':
        try:
            return json.loads(text)
        except json.JSONDecodeError as exc:
            raise YAMLError(f"line {line}: bad double-quoted string: {exc}") from exc
    inner = text[1:-1].replace("''", "'")
    return inner


def _scalar(text: str, line: int) -> Any:
    text = text.strip()
    if text == "":
        return None
    if text[0] in "[{":
        value, index = _Flow(text, line).parse()
        if index != len(text):
            raise YAMLError(f"line {line}: trailing flow content")
        return value
    if text[0] in "'\"":
        return _unquote(text, line)
    lowered = text.lower()
    if lowered in ("null", "~"):
        return None
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    if _INT.match(text):
        return int(text)
    if _FLOAT.match(text):
        return float(text)
    return text


class _Flow:
    def __init__(self, text: str, line: int):
        self.text = text
        self.line = line
        self.i = 0

    def parse(self) -> tuple[Any, int]:
        self._ws()
        value = self._value()
        self._ws()
        return value, self.i

    def _peek(self) -> str:
        return self.text[self.i] if self.i < len(self.text) else ""

    def _ws(self) -> None:
        while self.i < len(self.text) and self.text[self.i] in " \t":
            self.i += 1

    def _value(self) -> Any:
        ch = self._peek()
        if ch == "[":
            return self._seq()
        if ch == "{":
            return self._map()
        if ch in "'\"":
            return self._quoted()
        text = self._plain(",]}:", allow_colon=False)
        lowered = text.lower()
        if lowered in ("null", "~"):
            return None
        if lowered == "true":
            return True
        if lowered == "false":
            return False
        if _INT.match(text):
            return int(text)
        if _FLOAT.match(text):
            return float(text)
        return text

    def _quoted(self) -> str:
        quote = self.text[self.i]
        start = self.i
        self.i += 1
        while self.i < len(self.text):
            ch = self.text[self.i]
            if quote == '"' and ch == "\\":
                self.i += 2
                continue
            if ch == quote:
                if quote == "'" and self.i + 1 < len(self.text) and self.text[self.i + 1] == "'":
                    self.i += 2
                    continue
                self.i += 1
                return _unquote(self.text[start:self.i], self.line)
            self.i += 1
        raise YAMLError(f"line {self.line}: unterminated quoted string")

    def _plain(self, stops: str, allow_colon: bool = True) -> str:
        start = self.i
        while self.i < len(self.text):
            ch = self.text[self.i]
            if ch in stops:
                if ch == ":" and allow_colon:
                    if self.i + 1 < len(self.text) and self.text[self.i + 1] not in " \t":
                        self.i += 1
                        continue
                break
            self.i += 1
        return self.text[start:self.i].strip()

    def _seq(self) -> list[Any]:
        self.i += 1
        items: list[Any] = []
        while True:
            self._ws()
            if self._peek() == "]":
                self.i += 1
                return items
            if self._peek() == "":
                raise YAMLError(f"line {self.line}: unterminated flow sequence")
            items.append(self._value())
            self._ws()
            ch = self._peek()
            if ch == ",":
                self.i += 1
                continue
            if ch == "]":
                self.i += 1
                return items
            raise YAMLError(f"line {self.line}: expected ',' or ']'")

    def _map(self) -> dict[str, Any]:
        self.i += 1
        result: dict[str, Any] = {}
        while True:
            self._ws()
            if self._peek() == "}":
                self.i += 1
                return result
            if self._peek() == "":
                raise YAMLError(f"line {self.line}: unterminated flow mapping")
            if self._peek() in "'\"":
                key = self._quoted()
            else:
                key = self._plain(",:}", allow_colon=False)
            self._ws()
            if self._peek() != ":":
                raise YAMLError(f"line {self.line}: expected ':' in flow mapping")
            self.i += 1
            self._ws()
            result[str(key)] = self._value()
            self._ws()
            ch = self._peek()
            if ch == ",":
                self.i += 1
                continue
            if ch == "}":
                self.i += 1
                return result
            raise YAMLError(f"line {self.line}: expected ',' or '}}'")


def _split_key(text: str, line: int) -> tuple[str, str] | None:
    quote: str | None = None
    depth = 0
    i = 0
    while i < len(text):
        ch = text[i]
        if quote:
            if quote == '"' and ch == "\\":
                i += 2
                continue
            if ch == quote:
                if quote == "'" and i + 1 < len(text) and text[i + 1] == "'":
                    i += 2
                    continue
                quote = None
        else:
            if ch in "'\"":
                quote = ch
            elif ch in "[{":
                depth += 1
            elif ch in "]}":
                depth -= 1
            elif ch == ":" and depth == 0:
                if i + 1 == len(text) or text[i + 1] in " \t":
                    key = text[:i].strip()
                    if not key:
                        raise YAMLError(f"line {line}: empty key")
                    if key[0] in "'\"":
                        key = _unquote(key, line)
                    return key, text[i + 1 :].strip()
        i += 1
    return None


class _Lines:
    def __init__(self, text: str):
        self.items: list[tuple[int, str, int]] = []
        for number, raw in enumerate(text.replace("\r\n", "\n").replace("\r", "\n").split("\n"), 1):
            leading = raw[: len(raw) - len(raw.lstrip(" \t"))]
            if "\t" in leading:
                raise YAMLError(f"line {number}: tabs are not allowed for indentation")
            stripped = _strip_comment(raw)
            if not stripped.strip():
                continue
            indent = len(stripped) - len(stripped.lstrip(" "))
            self.items.append((indent, stripped.strip(), number))


def _parse_block(lines: list[tuple[int, str, int]], index: int, indent: int) -> tuple[Any, int]:
    text = lines[index][1]
    if text == "-" or text.startswith("- "):
        return _parse_sequence(lines, index, indent)
    return _parse_mapping(lines, index, indent)


def _parse_mapping(lines: list[tuple[int, str, int]], index: int, indent: int) -> tuple[dict[str, Any], int]:
    result: dict[str, Any] = {}
    while index < len(lines):
        ind, text, number = lines[index]
        if ind < indent:
            break
        if ind > indent:
            raise YAMLError(f"line {number}: unexpected indentation")
        if text == "-" or text.startswith("- "):
            break
        pair = _split_key(text, number)
        if pair is None:
            raise YAMLError(f"line {number}: expected 'key: value'")
        key, rest = pair
        if rest == "":
            if index + 1 < len(lines) and lines[index + 1][0] > indent:
                value, index = _parse_block(lines, index + 1, lines[index + 1][0])
            else:
                value = None
                index += 1
        else:
            value = _scalar(rest, number)
            index += 1
        result[key] = value
    return result, index


def _parse_sequence(lines: list[tuple[int, str, int]], index: int, indent: int) -> tuple[list[Any], int]:
    items: list[Any] = []
    while index < len(lines):
        ind, text, number = lines[index]
        if ind < indent:
            break
        if ind != indent or not (text == "-" or text.startswith("- ")):
            break
        rest = text[1:].strip()
        if rest == "":
            if index + 1 < len(lines) and lines[index + 1][0] > indent:
                value, index = _parse_block(lines, index + 1, lines[index + 1][0])
            else:
                value = None
                index += 1
            items.append(value)
            continue
        pair = _split_key(rest, number)
        if pair is not None:
            key, tail = pair
            item: dict[str, Any] = {}
            if tail == "":
                if index + 1 < len(lines) and lines[index + 1][0] > indent:
                    value, index = _parse_block(lines, index + 1, lines[index + 1][0])
                else:
                    value = None
                    index += 1
            else:
                value = _scalar(tail, number)
                index += 1
            item[key] = value
            if index < len(lines) and lines[index][0] > indent:
                more, index = _parse_block(lines, index, lines[index][0])
                if not isinstance(more, dict):
                    raise YAMLError(f"line {number}: sequence item mixes a mapping with a block")
                item.update(more)
            items.append(item)
            continue
        items.append(_scalar(rest, number))
        index += 1
    return items, index


def loads(text: str) -> Any:
    lines = _Lines(text)
    if not lines.items:
        return None
    value, index = _parse_block(lines.items, 0, lines.items[0][0])
    if index != len(lines.items):
        number = lines.items[index][2]
        raise YAMLError(f"line {number}: could not parse the rest of the document")
    return value
