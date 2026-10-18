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


#: A ``typedef`` whose declarator has no brace (so the aggregation typedefs in
#: :mod:`mtm.gen` handle the struct/union/array forms) and ends at the line's
#: ``;``. Group ``decl`` is ``<type specifiers + stars> <name>``. The statement may
#: start a line *or* follow a previous declaration on the same line
#: (``struct S { … }; typedef struct S T;``) — an anchor of ``^[ \t]*`` alone would
#: miss the latter and leave the alias unresolved, which downstream would emit as
#: uncompilable C.
_TYPEDEF_RE = re.compile(r"(?m)(?:^[ \t]*|(?<=[;}])[ \t]*)typedef\s+(?P<decl>[^;{}\n]+?)\s*;")

#: An aggregate *definition* alias: ``typedef struct { ... } S;`` (or ``union``),
#: including the pointer form ``typedef struct { ... } *SP;``. Its ``{...}`` body
#: puts it outside :data:`_TYPEDEF_RE`; ``[^;{}]*`` admits a tag between ``struct``
#: and ``{`` but no nested braces in the (flat) fixture structs this scanner
#: supports. The optional ``*`` before the name keeps the pointer alias (whose
#: pointee is still the aggregate) in the same family.
_AGGREGATE_DEF_RE = re.compile(
    r"\btypedef\s+(?:struct|union)\b[^;{}]*\{[^}]*\}\s*\*?\s*(?P<name>[A-Za-z_]\w*)\s*;",
    re.DOTALL)
#: An alias of a struct/union *tag*: ``typedef struct Tag S;`` (or ``*S``). Like
#: :data:`_TYPEDEF_RE` it may follow an earlier declaration on the same line
#: (``struct Tag { … }; typedef struct Tag S;``).
_AGGREGATE_REF_RE = re.compile(
    r"(?m)(?:^[ \t]*|(?<=[;}])[ \t]*)typedef\s+(?:struct|union)\s+[A-Za-z_]\w*\s*\*?\s*"
    r"(?P<name>[A-Za-z_]\w*)\s*;")

#: Alias-name patterns for an **opaque** declarator, where the trailing-identifier
#: extraction cannot reach the name: a *function pointer* ``typedef void (*cb)(int);``
#: (``decl`` ends in ``)``) and a *function type* / *array* ``typedef int Func(int);``
#: / ``typedef int Arr[4];`` (the name is the identifier before the first
#: ``(``/``[``). Both are recorded opaque so a parameter using the alias is refused
#: with a named limitation rather than emitted as an uncompilable cast — the refusal
#: the ``opaque`` marker exists for.
_FNPTR_ALIAS_RE = re.compile(r"\(\s*\*\s*(?P<name>[A-Za-z_]\w*)\s*\)\s*\(")
_ARRAY_ALIAS_RE = re.compile(r"(?P<name>[A-Za-z_]\w*)\s*\[")


class Typedef:
    """A resolved ``typedef``: its aliased **pointee base** and ``*`` depth.

    ``depth == -1`` marks a declarator this scanner will not resolve (a
    function-pointer or array typedef): using such an alias as a parameter type
    must be refused rather than mis-rendered, so the depth is kept as an explicit
    "opaque" marker instead of guessed.

    ``aggregate`` marks an alias whose *ultimate* base is an aggregate — a
    ``struct``/``union``/array, or another alias thereof (``typedef Sample Q;``
    after ``typedef struct {...} Sample;``). A pointer to such a type cannot be
    initialised from a scalar, so a parameter using it must be refused: the plain
    ``aggregate_aliases`` scan only sees the *inline* form and would let the
    chained alias emit ``Q out0 = (Q)9;``, which does not compile.
    """

    __slots__ = ("base", "depth", "opaque", "aggregate")

    def __init__(self, base: str, depth: int, opaque: bool = False,
                 aggregate: bool = False):
        self.base = base
        self.depth = depth
        self.opaque = opaque
        self.aggregate = aggregate


def scan_typedefs(path: Path) -> dict[str, Typedef]:
    """Map each ``typedef`` alias in *path* to its resolved :class:`Typedef`.

    A pointer ``typedef`` is the shape the C type→argument chain must respect: a
    parameter declared ``P *pp`` with ``typedef int *P`` is an ``int **`` and must
    be materialised at depth 2, not depth 1 (the F3 defect). Resolving the alias
    lets the generator keep the *depth* rather than emit ``P v = (P)scalar``,
    which does not compile. A function-pointer/array typedef is recorded as
    opaque so a parameter using it is refused with a named limitation.
    """
    raw = path.read_text(encoding="utf-8", errors="replace")
    clean = strip_comments_and_strings(raw)
    found: dict[str, Typedef] = {}
    for m in _TYPEDEF_RE.finditer(clean):
        decl = m.group("decl").strip()
        if not decl or "(" in decl or "[" in decl:
            # An opaque declarator (function pointer / array / unparseable). A
            # trailing identifier does not reach the alias name here — a function
            # pointer's ``decl`` ends in ``)`` and an array's in ``]`` — so match
            # the alias position explicitly; missing them left the refusal dead.
            opaque_name = None
            if "(" in decl:
                fn = _FNPTR_ALIAS_RE.search(decl)
                opaque_name = fn.group("name") if fn else None
            if opaque_name is None and ("(" in decl or "[" in decl):
                # A function *type* alias (`typedef int Func(int);`) or an array:
                # the alias name is the identifier immediately before the first
                # declarator bracket/paren.
                lead = re.search(r"([A-Za-z_]\w*)\s*[\(\[]", decl)
                opaque_name = lead.group(1) if lead else None
            if opaque_name is None:
                tail = re.search(r"([A-Za-z_]\w*)\s*$", decl)
                opaque_name = tail.group(1) if tail else None
            if opaque_name:
                found[opaque_name] = Typedef("", -1, opaque=True)
            continue
        segments = decl.split("*")
        depth = len(segments) - 1
        base_tokens = [t for t in segments[0].split()]
        tail = segments[-1].split()
        if not tail:
            continue
        name = tail[-1]
        # A trailing token that is itself only the name; the base is the leading
        # segment minus storage-class specifiers (a `typedef` declarator never
        # carries a storage class, but `_STORAGE_CLASS` tokens are harmless here).
        base = " ".join(t for t in base_tokens if t != name).strip()
        if not base and depth == 0:
            base = "int"
        found[name] = Typedef(base, depth)
    _mark_aggregate_aliases(found, clean)
    return found


def _mark_aggregate_aliases(found: dict[str, Typedef], clean: str) -> None:
    """Set ``aggregate`` on aliases whose ultimate base is a struct/union.

    Runs after the whole file is scanned so an alias *of* an alias is resolved even
    when ``typedef Sample Q;`` is declared before ``Sample`` itself (the fixpoint
    loop below convergences either way). Only struct/union is chased: a scalar
    alias (``typedef int myint``) is a legitimate scalar pointee that must **not**
    be refused, and an array base is not reachable through a plain alias token.

    Three declaration forms must all be caught, because missing any one lets a
    parameter pointing at the aggregate be treated as a scalar and emit
    ``Q out0 = (Q)9;``, which does not compile (plan §C):

    * ``typedef struct {...} S;``  — an inline struct/union *definition*;
    * ``typedef struct Tag S;``    — an alias of a struct/union tag;
    * ``typedef S Q;``             — an alias *of* an alias (chained).
    """
    # 1. Inline definition: `typedef struct { ... } Name;`. Its `{...}` body keeps
    #    it out of `_TYPEDEF_RE`, so it is matched separately here.
    for m in _AGGREGATE_DEF_RE.finditer(clean):
        found.setdefault(m.group("name"), Typedef("", 0)).aggregate = True
    # 2. Alias of a struct/union *tag*: `typedef struct Tag Alias;`. A reference to
    #    an untyped-brace-free `struct Tag` is always an aggregate.
    for m in _AGGREGATE_REF_RE.finditer(clean):
        found.setdefault(m.group("name"), Typedef("", 0)).aggregate = True
    # 3. Chained alias: `typedef Sample Q;` where Sample is (becomes) aggregate.
    #    Iterate to a fixpoint so an arbitrarily long chain collapses in either
    #    declaration order.
    changed = True
    while changed:
        changed = False
        for typedef in found.values():
            if typedef.aggregate or typedef.opaque:
                continue
            base = typedef.base.strip()
            if base in found and found[base].aggregate and not found[base].opaque:
                typedef.aggregate = True
                changed = True


#: Every `typedef ...;` statement (including an inline struct/union body), so a
#: declaration that *uses* an alias can carry that alias's own definition. As with
#: :data:`_TYPEDEF_RE` the statement may follow a previous one on the same line.
_TYPEDEF_STMT_RE = re.compile(
    r"(?m)(?:^[ \t]*|(?<=[;}])[ \t]*)typedef\b[^;{}]*(?:\{[^}]*\}[^;{}]*)?;")


def typedef_statement_map(text: str) -> dict[str, str]:
    """Map each alias *name* in *text* to the verbatim statement defining it.

    Used to make a private interface header self-sufficient: every alias its
    declaration mentions is prepended from the sources it was derived from.
    """
    clean = strip_comments_and_strings(text)
    result: dict[str, str] = {}
    for m in _TYPEDEF_STMT_RE.finditer(clean):
        statement = m.group(0).strip()
        # An inline struct/union body: the alias is the identifier after ``}``.
        decl = re.sub(r"^\s*typedef\b", "", statement).rstrip(";").strip()
        body = re.search(r"\}(?P<rest>[^;]*)$", decl)
        if body:
            name_m = re.search(r"([A-Za-z_]\w*)\s*$", body.group("rest"))
            if name_m:
                result.setdefault(name_m.group(1), statement)
            continue
        name = None
        if "(" in decl:
            fn = _FNPTR_ALIAS_RE.search(decl)
            name = fn.group("name") if fn else None
        if name is None and "[" in decl:
            arr = _ARRAY_ALIAS_RE.search(decl)
            name = arr.group("name") if arr else None
        if name is None:
            tail = re.search(r"([A-Za-z_]\w*)\s*$", decl)
            name = tail.group(1) if tail else None
        if name:
            result.setdefault(name, statement)
    return result


def scan_typedefs_of(paths: "list[Path] | tuple[Path, ...]") -> dict[str, Typedef]:
    """Merge :func:`scan_typedefs` over several header/declaration files.

    Later files do not silently override an earlier alias to a *different*
    meaning; the first definition wins so an include-order surprise cannot change
    a depth mid-suite (the suite compiles one project, so one meaning per alias).
    """
    merged: dict[str, Typedef] = {}
    for path in paths:
        for name, typedef in scan_typedefs(path).items():
            merged.setdefault(name, typedef)
    return merged


def _line_of(text: str, offset: int) -> int:
    return text.count("\n", 0, offset) + 1
