"""Deterministic C scanning helpers.

Small, dependency-free lexical helpers used by the inventory and feasibility
stages: resolve ``#include`` directives, extract top-level function definitions
and signature, and find identifiers *called* but not defined in a translation
unit (candidate link-time externals).

Deliberately not a full C parser: it is a conservative, auditable scanner whose
only job is to produce **deterministic** facts (paths, symbol names, line
numbers) that later stages and the LLM prompt can rely on. Anything it cannot
decide is reported as unknown rather than guessed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

#: C keywords that must never be mistaken for a function definition or a call.
_KEYWORDS = {
    "if", "for", "while", "switch", "return", "sizeof", "do", "else", "case",
    "break", "continue", "goto", "typedef", "struct", "union", "enum", "static",
    "inline", "extern", "const", "volatile", "unsigned", "signed", "void",
    "char", "short", "int", "long", "float", "double", "defined", "assert",
}

#: Control-flow keywords: a ``name(...) {`` whose name is one of these is a
#: statement, never a function definition. Deliberately excludes type names so a
#: definition returning ``int``/``void``/``char`` is not rejected.
_CONTROL_KEYWORDS = {
    "if", "for", "while", "switch", "return", "sizeof", "do", "else", "case",
    "break", "continue", "goto", "defined", "assert",
}

#: Common C standard-library call names, excluded from the "unresolved external"
#: set because they link from libc. Kept small and explicit; an unknown name is
#: reported as an external candidate rather than silently assumed to be stdlib.
_STDLIB_CALLS = {
    "printf", "fprintf", "sprintf", "snprintf", "puts", "putchar", "getchar",
    "malloc", "calloc", "realloc", "free", "memcpy", "memmove", "memset",
    "memcmp", "strlen", "strcmp", "strncmp", "strcpy", "strncpy", "strchr",
    "strrchr", "strstr", "abort", "exit", "atoi", "strtol", "strtoul", "qsort",
}

_INCLUDE_RE = re.compile(r'^\s*#\s*include\s*([<"])([^>"]+)([>"])', re.MULTILINE)
_DEF_RE = re.compile(
    r"(?m)^[ \t]*(?P<ret>(?:[A-Za-z_]\w*[ \t\*]+)+?)(?P<name>[A-Za-z_]\w*)"
    r"[ \t]*\((?P<params>[^;{}()]*)\)[ \t\r\n]*\{"
)
_CALL_RE = re.compile(r"\b([A-Za-z_]\w*)\s*\(")


def strip_comments_and_strings(text: str) -> str:
    """Remove comments and string/char literals, preserving newlines.

    Preserving line structure keeps line numbers stable for source anchors.
    """
    out: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        nxt = text[i + 1] if i + 1 < n else ""
        if ch == "/" and nxt == "/":
            while i < n and text[i] != "\n":
                i += 1
        elif ch == "/" and nxt == "*":
            i += 2
            while i < n and not (text[i] == "*" and i + 1 < n and text[i + 1] == "/"):
                if text[i] == "\n":
                    out.append("\n")
                i += 1
            i += 2
        elif ch == '"' or ch == "'":
            quote = ch
            i += 1
            while i < n and text[i] != quote:
                if text[i] == "\\":
                    i += 1
                i += 1
            i += 1
        else:
            out.append(ch)
            i += 1
    return "".join(out)


@dataclass
class Include:
    """One ``#include`` directive."""

    name: str
    system: bool  # <...> vs "..."
    line: int


@dataclass
class FunctionDef:
    """A top-level function definition."""

    name: str
    return_type: str
    signature: str
    line: int


@dataclass
class CUnit:
    """A scanned translation unit (one ``.c`` file)."""

    path: Path
    includes: list[Include] = field(default_factory=list)
    functions: list[FunctionDef] = field(default_factory=list)
    called: set[str] = field(default_factory=set)

    @property
    def defined_names(self) -> set[str]:
        return {f.name for f in self.functions}


def scan_c(path: Path) -> CUnit:
    """Scan one C source file into a :class:`CUnit`."""
    raw = path.read_text(encoding="utf-8", errors="replace")
    clean = strip_comments_and_strings(raw)
    includes = [
        Include(name=m.group(2), system=(m.group(1) == "<"), line=_line_of(raw, m.start()))
        for m in _INCLUDE_RE.finditer(raw)
    ]
    functions: list[FunctionDef] = []
    for m in _DEF_RE.finditer(clean):
        name = m.group("name")
        ret = m.group("ret").strip()
        if name in _CONTROL_KEYWORDS or ret.split()[-1] in _CONTROL_KEYWORDS:
            continue
        params = " ".join(m.group("params").split()) or "void"
        functions.append(FunctionDef(
            name=name,
            return_type=ret,
            signature=f"{ret} {name}({params})",
            line=_line_of(clean, m.start()),
        ))
    called = {m.group(1) for m in _CALL_RE.finditer(clean)}
    return CUnit(path=path, includes=includes, functions=functions, called=called)


def external_calls(unit: CUnit) -> list[str]:
    """Identifiers called in the unit but neither defined there nor in libc.

    These are the candidate link-time externals that require a generated mock or
    shim for host execution. Sorted for determinism.
    """
    externals = unit.called - unit.defined_names - _KEYWORDS - _STDLIB_CALLS
    return sorted(externals)


_PROTO_RE = re.compile(
    r"(?m)^[ \t]*(?P<ret>(?:[A-Za-z_]\w*[ \t\*]+)+?)(?P<name>[A-Za-z_]\w*)"
    r"[ \t]*\((?P<params>[^;{}()]*)\)[ \t\r\n]*;"
)


def scan_prototypes(path: Path) -> list[tuple[str, str]]:
    """Extract ``(name, declaration)`` for each top-level prototype in *path*.

    Used to derive a mocked symbol's exact declaration from a **selected
    header** deterministically, instead of asking the model to restate an
    interface it might paraphrase incorrectly.
    """
    raw = path.read_text(encoding="utf-8", errors="replace")
    clean = strip_comments_and_strings(raw)
    found: list[tuple[str, str]] = []
    for m in _PROTO_RE.finditer(clean):
        name = m.group("name")
        ret = m.group("ret").strip()
        if name in _CONTROL_KEYWORDS or ret.split()[-1] in _CONTROL_KEYWORDS:
            continue
        params = " ".join(m.group("params").split()) or "void"
        found.append((name, f"{ret} {name}({params});"))
    return found


def _line_of(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1
