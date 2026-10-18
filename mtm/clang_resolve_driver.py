"""In-image libclang driver for :mod:`mtm.ctypes_clang`.

This module is the **only** place that imports ``clang.cindex``. It runs *inside*
the product image (the host has no clang) and is executed as
``python3 -m mtm.clang_resolve_driver`` with ``PYTHONPATH=/opt/mtm`` and a JSON
spec on stdin; it prints a JSON result on stdout. Keeping it a shipped package
module (rather than a script the caller writes at run time) means the adapter
never writes into the read-only bench mount and there is exactly one auditable
driver to review.

The driver is deliberately shallow: it translates libclang cursors/types into a
small JSON fact set and never decides product policy. Failure *classification*
(``BLOCKED`` vs ``TOOL_FAILED``) lives in :mod:`mtm.ctypes_clang`, which is
host-testable; the driver only reports what libclang saw.

An **opaque** shape (a function-pointer or array type, however deeply pointed)
is a *resolved fact* — the ``opaque`` flag on the type — not a resolution
failure: lot B's generator consumes it to emit its existing named limitation.
Only an incomplete **build context** (a missing header/define/include root)
makes resolution impossible, reported as a ``MISSING_CONTEXT`` issue the host
adapter maps to ``BLOCKED``.

Importing this module on the host must not fail (only ``main`` touches libclang),
so that the package stays importable where clang is absent.
"""

from __future__ import annotations

import json
import sys
from typing import Any

#: Issue kind: the effective include/define/target context is incomplete, so the
#: declaration could not be resolved. Consumed by :mod:`mtm.ctypes_clang`.
MISSING_CONTEXT = "MISSING_CONTEXT"

#: Type kinds that name a record (``struct``/``union``/class) or an enumeration.
_RECORD_KINDS = {"RECORD", "UNION", "ENUM"}

#: Type kinds the adapter treats as opaque (cannot be rendered as a scalar or a
#: pointer-to-scalar declarator): function types and arrays, reached through any
#: number of pointer levels (a function *pointer* is a pointer to FUNCTIONPROTO).
_OPAQUE_KINDS = {
    "FUNCTIONPROTO", "FUNCTIONNOPROTO",
    "CONSTANTARRAY", "INCOMPLETEARRAY", "VARIABLEARRAY", "DEPENDENTSIZEDARRAY",
}

_QUALIFIERS = ("const", "volatile", "restrict")


def _type_fact(clang: Any, type_obj: Any, tokens: list[str],
               typedefs: dict[str, Any], root: str) -> dict[str, Any]:
    """Translate one libclang ``Type`` into the adapter's JSON fact set.

    ``tokens`` are the declaration's tokens (used to attribute a used ``typedef``
    alias); ``typedefs`` maps every alias name declared under *root* to its
    ``TYPEDEF_DECL`` cursor. Pointer depth and the canonical base are read from
    the **canonical** type, so a pointer alias resolves to its true depth (the F3
    case: ``typedef int *P; P *pp`` is ``int **``, depth 2).
    """
    canonical = type_obj.get_canonical()
    depth = 0
    pointee = canonical
    while pointee.kind.name == "POINTER":
        depth += 1
        pointee = pointee.get_pointee().get_canonical()

    base_spelling = pointee.spelling
    qualifiers = [q for q in _QUALIFIERS if q in base_spelling.split()]

    record: dict[str, Any] | None = None
    if pointee.kind.name in _RECORD_KINDS:
        decl = pointee.get_declaration()
        record = {"kind": pointee.kind.name, "tag": decl.spelling or ""}

    opaque = pointee.kind.name in _OPAQUE_KINDS

    aliases: list[dict[str, Any]] = []
    for name in dict.fromkeys(t for t in tokens if t in typedefs):
        cursor = typedefs[name]
        underlying = cursor.underlying_typedef_type.get_canonical()
        decl = underlying.get_declaration()
        ext = cursor.extent
        inline_body = bool(
            decl.location.file
            and ext.start.line <= decl.location.line <= ext.end.line
        )
        at = underlying
        while at.kind.name == "POINTER":
            at = at.get_pointee().get_canonical()
        aliases.append({
            "name": name,
            "file": _relpath(cursor.location.file.name if cursor.location.file else "", root),
            "line": cursor.location.line,
            "inline_body": inline_body,
            "opaque": at.kind.name in _OPAQUE_KINDS,
        })

    return {
        "spelling": type_obj.spelling,
        "canonical": canonical.spelling,
        "kind": canonical.kind.name,
        "base": base_spelling,
        "depth": depth,
        "qualifiers": qualifiers,
        "record": record,
        "opaque": opaque,
        "aliases": aliases,
    }


def _relpath(path: str, root: str) -> str:
    """Best-effort mount-relative path so facts are stable across containers."""
    if root and path.startswith(root.rstrip("/") + "/"):
        return path[len(root.rstrip("/")) + 1:]
    return path


def _collect_typedefs(clang: Any, tu: Any, root: str) -> dict[str, Any]:
    """Alias name -> ``TYPEDEF_DECL`` for every alias declared under *root*.

    Restricted to the mounted tree so a system header's typedefs (``uint32_t``)
    do not drown the facts; only aliases the *selected* build context defines are
    attributable to a defining declaration location.
    """
    found: dict[str, Any] = {}
    for cursor in tu.cursor.walk_preorder():
        if cursor.kind != clang.CursorKind.TYPEDEF_DECL or not cursor.spelling:
            continue
        loc = cursor.location.file
        if loc is None or (root and not loc.name.startswith(root.rstrip("/") + "/")):
            continue
        found.setdefault(cursor.spelling, cursor)
    return found


def _resolve_one(clang: Any, tu: Any, root: str) -> dict[str, Any]:
    """Function declarations located under *root*, with their type facts."""
    typedefs = _collect_typedefs(clang, tu, root)
    functions: list[dict[str, Any]] = []
    for cursor in tu.cursor.walk_preorder():
        if cursor.kind != clang.CursorKind.FUNCTION_DECL or not cursor.spelling:
            continue
        loc = cursor.location.file
        if loc is None or (root and not loc.name.startswith(root.rstrip("/") + "/")):
            continue
        entry: dict[str, Any] = {
            "name": cursor.spelling,
            "file": _relpath(loc.name, root),
            "line": cursor.location.line,
            "variadic": cursor.type.is_function_variadic(),
            "return": _type_fact(clang, cursor.result_type, [], typedefs, root),
            "params": [
                _type_fact(clang, arg.type, [t.spelling for t in arg.get_tokens()],
                           typedefs, root)
                for arg in cursor.get_arguments()
            ],
        }
        functions.append(entry)
    return {"functions": functions}


def _severity_name(severity: Any) -> str:
    """A readable severity; some bindings return an int, others an enum member."""
    name = getattr(severity, "name", None)
    if isinstance(name, str):
        return name
    return {
        0: "Ignored", 1: "Note", 2: "Warning", 3: "Error", 4: "Fatal",
    }.get(int(severity), str(severity))


def _diagnostics(tu: Any) -> tuple[list[str], list[dict[str, Any]]]:
    """Split diagnostics into renderable lines and structured issues."""
    messages: list[str] = []
    issues: list[dict[str, Any]] = []
    for diag in tu.diagnostics:
        text = diag.spelling
        loc = diag.location
        where = ""
        if loc.file:
            where = f"{loc.file.name}:{loc.line}:{loc.column}: "
        messages.append(f"{where}{_severity_name(diag.severity)}: {text}")
        if "not found" in text and ("file not found" in text or "'" in text):
            issues.append({"kind": MISSING_CONTEXT, "message": text,
                           "file": loc.file.name if loc.file else "",
                           "line": loc.line})
    return messages, issues


def run(spec: dict[str, Any]) -> dict[str, Any]:
    """Parse the spec's inputs and return the JSON fact set (never raises).

    A failure to *load* libclang is returned as a ``tool_error`` the host adapter
    maps to ``TOOL_FAILED``; a parse with an incomplete context yields the
    structured ``MISSING_CONTEXT`` issues (→ ``BLOCKED``) instead.
    """
    try:
        import clang.cindex as clang  # noqa: PLC0415 - only exists inside the image
        index = clang.Index.create()
    except Exception as exc:  # noqa: BLE001 - reported as a tool fault, not a crash
        return {"ok": False, "tool_error": f"{type(exc).__name__}: {exc}"}

    root = spec.get("root", "/src")
    args = ["-xc"]
    args += [f"-I{d}" for d in spec.get("include_dirs", [])]
    args += [f"-D{d}" for d in spec.get("defines", [])]
    args += list(spec.get("args", []))

    functions: list[dict[str, Any]] = []
    messages: list[str] = []
    issues: list[dict[str, Any]] = []
    for path in spec.get("inputs", []):
        try:
            tu = index.parse(path, args=list(args))
        except Exception as exc:  # noqa: BLE001 - a load error is missing context
            issues.append({"kind": MISSING_CONTEXT,
                           "message": f"could not parse {path}: {exc}",
                           "file": path, "line": 0})
            continue
        msgs, issues_here = _diagnostics(tu)
        messages.extend(msgs)
        issues.extend(issues_here)
        functions.extend(_resolve_one(clang, tu, root)["functions"])

    # One declaration may be reached through several inputs (a ``.c`` including a
    # header); keep the first sighting so the fact set has one entry per
    # declaration location rather than a duplicate per input.
    unique: dict[tuple[str, str, int], dict[str, Any]] = {}
    for fn in functions:
        unique.setdefault((fn["name"], fn["file"], fn["line"]), fn)
    functions = list(unique.values())

    version = _libclang_version(clang)

    return {
        "ok": not issues,
        "clang_version": version,
        "functions": functions,
        "issues": issues,
        "diagnostics": messages,
    }


def _libclang_version(clang: Any) -> str:
    """The loaded libclang's version string, or ``""`` when unavailable.

    The Debian binding registers ``clang_getClangVersion`` with a truncated
    pointer return type, so calling it through ``clang.cindex`` raises. Re-open
    the very library the binding loaded (``conf.lib._name``) with a correct
    signature; a version string is provenance metadata, so any failure degrades
    to ``""`` rather than failing resolution.
    """
    import ctypes

    try:
        name = getattr(clang.conf.lib, "_name", "")
        if not name:
            return ""
        raw = ctypes.CDLL(name)
        raw.clang_getClangVersion.restype = ctypes.c_void_p
        raw.clang_getClangVersion.argtypes = []
        raw.clang_getCString.restype = ctypes.c_char_p
        raw.clang_getCString.argtypes = [ctypes.c_void_p]
        text = raw.clang_getCString(raw.clang_getClangVersion())
        return text.decode() if isinstance(text, bytes) else str(text or "")
    except Exception:  # noqa: BLE001 - version is informational only
        return ""


def main() -> int:
    """Read a JSON spec on stdin, print a JSON result on stdout."""
    raw = sys.stdin.read()
    try:
        spec = json.loads(raw or "{}")
    except json.JSONDecodeError as exc:
        print(json.dumps({"ok": False, "tool_error": f"bad spec: {exc}"}))
        return 0
    result = run(spec)
    print(json.dumps(result))
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised inside the image
    raise SystemExit(main())
