"""The rule language, and the glob matcher underneath it.

A rule is one line:

    package.json => package-lock.json

Read it as *if the left side changed, the right side must change too*. The
arrow points at the thing that has to keep up. Direction is the whole point:
editing a manifest without regenerating its lockfile is a bug, and updating
a lockfile on its own is a Tuesday.

``<=>`` is sugar for a pair of rules, one each way, for the genuinely
symmetric cases.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

SYMMETRIC = "<=>"
DIRECTED = "=>"


class RuleError(ValueError):
    """A rule could not be parsed. Carries where it came from."""

    def __init__(self, message: str, *, source: str, lineno: int | None = None):
        super().__init__(message)
        self.message = message
        self.source = source
        self.lineno = lineno

    def where(self) -> str:
        return f"{self.source}:{self.lineno}" if self.lineno else self.source


@dataclass(frozen=True)
class Pattern:
    """A repo-relative path glob, compiled once."""

    text: str
    regex: re.Pattern[str]

    def matches(self, path: str) -> bool:
        return self.regex.fullmatch(path) is not None

    def select(self, paths) -> list[str]:
        return [p for p in paths if self.matches(p)]


@dataclass(frozen=True)
class Rule:
    """If ``trigger`` changed, ``required`` must have changed as well."""

    trigger: Pattern
    required: Pattern
    source: str  # where the rule came from, for error messages
    lineno: int | None = None

    @property
    def label(self) -> str:
        return f"{self.trigger.text} {DIRECTED} {self.required.text}"


def compile_pattern(text: str, *, source: str, lineno: int | None = None) -> Pattern:
    """Compile a glob into an anchored regex over whole repo-relative paths.

    The rules, in full, because half-remembered glob semantics are how you end
    up with a gate that silently matches nothing:

    * ``*`` matches within one path segment and never crosses ``/``.
    * ``**`` crosses ``/``. ``docs/**`` is everything under ``docs``.
    * ``?`` is one character, not ``/``.
    * ``[abc]`` and ``[!abc]`` are character classes.
    * A trailing ``/`` means the directory and everything in it.
    * Everything is matched against the *whole* path from the repo root.
      ``Makefile`` is the one at the root and nothing else; for any Makefile
      anywhere, write ``**/Makefile``.
    """
    raw = text.strip()
    if not raw:
        raise RuleError("empty pattern", source=source, lineno=lineno)
    if raw.startswith("/"):
        raw = raw.lstrip("/")
        if not raw:
            raise RuleError("empty pattern", source=source, lineno=lineno)
    if raw.endswith("/"):
        raw = raw + "**"

    out: list[str] = []
    i = 0
    n = len(raw)
    while i < n:
        char = raw[i]
        if char == "*":
            if raw.startswith("**", i):
                if raw.startswith("**/", i):
                    # Zero or more leading directories, so `**/x` matches a
                    # bare `x` at the root as well as `a/b/x`.
                    out.append("(?:[^/]+/)*")
                    i += 3
                    continue
                out.append(".*")
                i += 2
                continue
            out.append("[^/]*")
            i += 1
            continue
        if char == "?":
            out.append("[^/]")
            i += 1
            continue
        if char == "[":
            close = raw.find("]", i + 2)
            if close == -1:
                raise RuleError(
                    f"unclosed [ in pattern {text.strip()!r}",
                    source=source,
                    lineno=lineno,
                )
            body = raw[i + 1 : close]
            if body.startswith("!"):
                body = "^" + body[1:]
            out.append("[" + body.replace("\\", "\\\\") + "]")
            i = close + 1
            continue
        out.append(re.escape(char))
        i += 1

    return Pattern(text=text.strip(), regex=re.compile("".join(out)))


def parse_rule(line: str, *, source: str, lineno: int | None = None) -> list[Rule]:
    """Parse one rule line. Returns two rules for ``<=>``, one otherwise."""
    text = line.split("#", 1)[0].strip()
    if not text:
        return []

    if SYMMETRIC in text:
        left, _, right = text.partition(SYMMETRIC)
        both = True
    elif DIRECTED in text:
        left, _, right = text.partition(DIRECTED)
        both = False
    else:
        raise RuleError(
            f"no arrow in rule {text!r} — a rule looks like "
            f"'package.json {DIRECTED} package-lock.json'",
            source=source,
            lineno=lineno,
        )

    if DIRECTED in right or SYMMETRIC in right:
        raise RuleError(
            f"more than one arrow in rule {text!r} — one rule per line",
            source=source,
            lineno=lineno,
        )

    a = compile_pattern(left, source=source, lineno=lineno)
    b = compile_pattern(right, source=source, lineno=lineno)
    rules = [Rule(trigger=a, required=b, source=source, lineno=lineno)]
    if both:
        rules.append(Rule(trigger=b, required=a, source=source, lineno=lineno))
    return rules


def parse_rules(text: str, *, source: str) -> list[Rule]:
    """Parse a whole rules file. Blank lines and ``#`` comments are skipped."""
    rules: list[Rule] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        rules.extend(parse_rule(line, source=source, lineno=lineno))
    return rules
