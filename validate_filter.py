#!/usr/bin/env python3
"""Lightweight, zero-dependency structural validator for RS2F filter files."""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Position:
    line: int
    column: int


@dataclass(frozen=True)
class Issue:
    position: Position
    message: str


OPEN_TO_CLOSE = {"(": ")", "[": "]", "{": "}"}
CLOSE_TO_OPEN = {value: key for key, value in OPEN_TO_CLOSE.items()}
DEFINE_RE = re.compile(r"^\s*#define\s+([A-Za-z_][A-Za-z0-9_]*)")
PROJECT_MACRO_RE = re.compile(r"\b(?:VAR|CONST|MIN)_[A-Z0-9_]+\b")


def scan_structure(text: str) -> list[Issue]:
    issues: list[Issue] = []
    stack: list[tuple[str, Position]] = []
    line = column = 1
    index = 0
    string_start: Position | None = None
    block_comment_start: Position | None = None

    while index < len(text):
        char = text[index]
        following = text[index + 1] if index + 1 < len(text) else ""

        if block_comment_start:
            if char == "*" and following == "/":
                block_comment_start = None
                index += 2
                column += 2
                continue
        elif string_start:
            if char == '"':
                string_start = None
        elif char == "/" and following == "/":
            newline = text.find("\n", index)
            if newline == -1:
                break
            index = newline
            char = "\n"
        elif char == "/" and following == "*":
            block_comment_start = Position(line, column)
            index += 2
            column += 2
            continue
        elif char == '"':
            string_start = Position(line, column)
        elif char in OPEN_TO_CLOSE:
            stack.append((char, Position(line, column)))
        elif char in CLOSE_TO_OPEN:
            if not stack:
                issues.append(Issue(Position(line, column), f"orphaned closing {char!r}"))
            elif stack[-1][0] != CLOSE_TO_OPEN[char]:
                opening, position = stack.pop()
                issues.append(
                    Issue(
                        Position(line, column),
                        f"closing {char!r} does not match {opening!r} from "
                        f"{position.line}:{position.column}",
                    )
                )
            else:
                stack.pop()

        if char == "\n":
            line += 1
            column = 1
        else:
            column += 1
        index += 1

    if string_start:
        issues.append(Issue(string_start, "unterminated string literal"))
    if block_comment_start:
        issues.append(Issue(block_comment_start, "unterminated block comment"))
    for opening, position in stack:
        issues.append(Issue(position, f"unclosed {opening!r}"))
    return issues


def strip_strings_and_comments(text: str) -> str:
    result: list[str] = []
    index = 0
    in_string = False
    in_block_comment = False
    while index < len(text):
        char = text[index]
        following = text[index + 1] if index + 1 < len(text) else ""
        if in_block_comment:
            if char == "*" and following == "/":
                result.extend("  ")
                index += 2
                in_block_comment = False
            else:
                result.append("\n" if char == "\n" else " ")
                index += 1
        elif in_string:
            result.append("\n" if char == "\n" else " ")
            in_string = char != '"'
            index += 1
        elif char == "/" and following == "/":
            newline = text.find("\n", index)
            if newline == -1:
                result.extend(" " * (len(text) - index))
                break
            result.extend(" " * (newline - index))
            index = newline
        elif char == "/" and following == "*":
            result.extend("  ")
            index += 2
            in_block_comment = True
        elif char == '"':
            result.append(" ")
            index += 1
            in_string = True
        else:
            result.append(char)
            index += 1
    return "".join(result)


def scan_directives_and_macros(text: str) -> list[Issue]:
    issues: list[Issue] = []
    lines = text.splitlines()
    defined: set[str] = set()
    line_index = 0

    while line_index < len(lines):
        line = lines[line_index]
        if re.match(r"^\s*#", line) and not re.match(r"^\s*#define\b", line):
            issues.append(Issue(Position(line_index + 1, 1), "unknown preprocessor directive"))
            line_index += 1
            continue

        match = DEFINE_RE.match(line)
        if not match:
            line_index += 1
            continue

        defined.add(match.group(1))
        macro_start = line_index
        macro_lines = [line]
        while macro_lines[-1].rstrip().endswith("\\") and line_index + 1 < len(lines):
            line_index += 1
            macro_lines.append(lines[line_index])

        macro_text = "\n".join(macro_lines)
        macro_structure = scan_structure(strip_strings_and_comments(macro_text))
        for issue in macro_structure:
            issues.append(
                Issue(
                    Position(macro_start + issue.position.line, issue.position.column),
                    f"macro {match.group(1)}: {issue.message}; a previous line may be missing '\\'",
                )
            )
        line_index += 1

    semantic_text = strip_strings_and_comments(text)
    references = set(PROJECT_MACRO_RE.findall(semantic_text))
    for name in sorted(references - defined):
        match = re.search(rf"\b{re.escape(name)}\b", semantic_text)
        prefix = semantic_text[: match.start()] if match else ""
        line = prefix.count("\n") + 1
        column = len(prefix.rsplit("\n", 1)[-1]) + 1
        issues.append(Issue(Position(line, column), f"undefined project macro {name}"))

    return issues


def validate(path: Path) -> list[Issue]:
    text = path.read_text(encoding="utf-8").replace("\r\n", "\n").replace("\r", "\n")
    issues = scan_structure(text)
    issues.extend(scan_directives_and_macros(text))
    return sorted(set(issues), key=lambda issue: (issue.position.line, issue.position.column, issue.message))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("filter", nargs="?", type=Path, default=Path("bemfilter.rs2f"))
    args = parser.parse_args()

    try:
        issues = validate(args.filter)
    except (OSError, UnicodeError) as error:
        print(f"error: cannot read {args.filter}: {error}", file=sys.stderr)
        return 2

    if issues:
        for issue in issues:
            print(
                f"{args.filter}:{issue.position.line}:{issue.position.column}: {issue.message}",
                file=sys.stderr,
            )
        print(f"Validation failed with {len(issues)} issue(s).", file=sys.stderr)
        return 1

    line_count = len(args.filter.read_text(encoding="utf-8").splitlines())
    print(f"Validation passed: {args.filter} ({line_count} lines)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
