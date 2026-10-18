"""Minimal libclang-backed C type/declaration resolution (PR 3, lot A).

This module is the **sole semantic authority** for C declarations and types in the
selected host-compilation context (owner decision 0017): pinned Clang 18 /
libclang 18 inside the product image resolves each function declaration's
canonical parameter/return types, pointer depth, qualifiers and record/enum
identity, plus each used alias's defining declaration location and whether that
declaration carries an inline ``{…}`` body. It deliberately **replaces** the
unsound regex/include-guard heuristics on this path — it is not a general C
parser and creates no parallel AST store.

Design notes (why this shape):

* **The host has no clang.** ``mtm/gen.py`` runs on the host while libclang lives
  in the product image, so the adapter shells out with the same ``docker run``
  convention as :mod:`mtm.ceedling` (``--rm``, ``--user`` from the invoking uid,
  ``-e HOME=/tmp``, the bench mounted read-only, ``--entrypoint python3``). The
  in-image work is a shipped package module, :mod:`mtm.clang_resolve_driver`,
  executed as ``python3 -m mtm.clang_resolve_driver`` with the package importable
  at ``/opt/mtm``; **no file is written into the mounted bench**.

* **Testable seam.** ``resolver`` may be injected, so ``tests/`` exercises the
  parsing/mapping and the failure classification offline; the Docker path is
  covered by the execution-tier integration tests.

* **Typed failure, never a guess.** A missing SDK/header/build context (or a
  shape whose *resolution* needs it) maps to :data:`BLOCKED`
  (``Code.HOST_UNSUPPORTED``, a partial input limitation); a broken/absent clang
  tool or binding maps to :data:`TOOL_FAILED` (``Code.TOOL_FAILED``). An opaque
  function/array *shape* is not a failure — it is a resolved fact
  (``TypeFact.opaque``) the generator acts on. No unclassified exception ever
  reaches the generator, and a guessed declaration is never returned.
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Sequence

from .ceedling import CONTAINER_BENCH  # reuse the container bench mount point

#: Normalized resolution status (mirrors the feasibility vocabulary).
RESOLVED = "RESOLVED"
BLOCKED = "BLOCKED"
TOOL_FAILED = "TOOL_FAILED"

#: Where the in-image package lives (the Dockerfile's ``COPY mtm /opt/mtm/mtm``).
CONTAINER_PYTHONPATH = "/opt/mtm"

#: Entry command for the shipped in-image driver.
DRIVER_ARGV = ["-m", "mtm.clang_resolve_driver"]

#: Issue kind the driver emits for an incomplete build context (missing header,
#: define or include root). An opaque function/array *shape*, by contrast, is a
#: resolved fact (``TypeFact.opaque``), not a failure.
MISSING_CONTEXT = "MISSING_CONTEXT"


@dataclass
class AliasFact:
    """A ``typedef`` alias a declaration uses, and where it is defined."""

    name: str
    file: str
    line: int
    #: True when the alias's defining declaration has an inline ``{…}`` body
    #: (``typedef struct {…} S;`` / ``typedef enum {…} e_t;``) — such an alias
    #: cannot be re-declared and must be guarded/shared by its defining file.
    inline_body: bool = False
    #: True when the alias names a function-pointer/array shape (opaque).
    opaque: bool = False


@dataclass
class TypeFact:
    """One canonical C type as resolved by libclang."""

    spelling: str
    canonical: str
    kind: str
    base: str
    depth: int
    qualifiers: list[str] = field(default_factory=list)
    #: ``{"kind": "RECORD"|"ENUM", "tag": …}`` when the base names a record/enum.
    record: dict[str, str] | None = None
    #: True when the type is a function/array shape the generator cannot render.
    opaque: bool = False
    aliases: list[AliasFact] = field(default_factory=list)


@dataclass
class FunctionFact:
    """One resolved function declaration."""

    name: str
    file: str
    line: int
    return_type: TypeFact
    params: list[TypeFact] = field(default_factory=list)
    variadic: bool = False


@dataclass
class CTypeResolution:
    """The ``CTypeResolution`` fact set (``docs/architecture/data-contracts.md``).

    A single structured result for one resolution request: the resolved function
    declarations, the clang version used, raw diagnostics, and — when the request
    could not be resolved — a typed :data:`BLOCKED`/:data:`TOOL_FAILED` status
    with actionable diagnostics. ``functions`` is only ever populated on
    :data:`RESOLVED`; a blocked/tool-failed result never carries a guessed
    declaration.
    """

    status: str
    clang_version: str = ""
    functions: list[FunctionFact] = field(default_factory=list)
    diagnostics: list[str] = field(default_factory=list)
    issues: list[dict[str, Any]] = field(default_factory=list)
    message: str = ""

    @property
    def ok(self) -> bool:
        return self.status == RESOLVED

    def by_name(self, symbol: str) -> FunctionFact | None:
        """The resolved fact for *symbol*, or ``None`` when absent."""
        for fact in self.functions:
            if fact.name == symbol:
                return fact
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "clang_version": self.clang_version,
            "functions": [
                {
                    "name": f.name, "file": f.file, "line": f.line,
                    "variadic": f.variadic,
                    "return": _type_to_dict(f.return_type),
                    "params": [_type_to_dict(p) for p in f.params],
                }
                for f in self.functions
            ],
            "diagnostics": self.diagnostics,
            "issues": self.issues,
            "message": self.message,
        }


def _type_to_dict(t: TypeFact) -> dict[str, Any]:
    return {
        "spelling": t.spelling, "canonical": t.canonical, "kind": t.kind,
        "base": t.base, "depth": t.depth, "qualifiers": t.qualifiers,
        "record": t.record, "opaque": t.opaque,
        "aliases": [
            {"name": a.name, "file": a.file, "line": a.line,
             "inline_body": a.inline_body, "opaque": a.opaque}
            for a in t.aliases
        ],
    }


#: A resolver takes the driver spec (a plain dict) and returns the driver's JSON
#: result as a dict. The default shells out to the product image; tests inject a
#: stub to exercise mapping and classification offline.
Resolver = Callable[[dict[str, Any], "ResolveContext"], dict[str, Any]]


@dataclass
class ResolveContext:
    """Everything the default image-backed resolver needs to build its argv."""

    bench_root: Path
    image_ref: str
    user: str | None = None
    timeout: int = 300


def resolve(
    paths: Sequence[Path | str],
    *,
    include_dirs: Sequence[Path | str] = (),
    defines: Sequence[str] = (),
    args: Sequence[str] = (),
    bench_root: Path | str,
    image: str | None = None,
    user: str | None = None,
    timeout: int = 300,
    resolver: Resolver | None = None,
) -> CTypeResolution:
    """Resolve *paths* with libclang inside the product image.

    Returns a :class:`CTypeResolution`. *paths* are files under *bench_root*; the
    request mounts *bench_root* read-only at :data:`CONTAINER_BENCH` and reports
    the inputs by their container path. *image* defaults to the product image ref
    (``mtm.docker.image_ref()``). ``resolver`` overrides the image-backed
    transport (for host-side tests). Never raises for a resolution/tool failure:
    the typed status carries the reason.
    """
    bench = Path(bench_root).resolve()
    context = ResolveContext(bench_root=bench, image_ref=image or _default_image(),
                             user=user or _invoking_user() or "0:0", timeout=timeout)
    spec = {
        "root": CONTAINER_BENCH,
        "inputs": [_container_path(bench, p) for p in paths],
        "include_dirs": [_container_path(bench, d) for d in include_dirs],
        "defines": list(defines),
        "args": list(args),
    }
    runner = resolver or _image_resolver
    try:
        raw = runner(spec, context)
    except CTypeResolutionError as exc:
        return CTypeResolution(status=TOOL_FAILED, message=str(exc))
    except OSError as exc:
        return CTypeResolution(
            status=TOOL_FAILED, message=f"could not run the libclang driver: {exc}")
    return _map_result(raw)


def _container_path(bench: Path, path: Path | str) -> str:
    """Map a bench-relative or absolute host path to its container path."""
    candidate = Path(path)
    if candidate.is_absolute():
        try:
            rel = candidate.resolve().relative_to(bench)
        except ValueError:
            return candidate.as_posix()
        return f"{CONTAINER_BENCH}/{rel.as_posix()}"
    return f"{CONTAINER_BENCH}/{candidate.as_posix()}"


def _map_result(raw: dict[str, Any]) -> CTypeResolution:
    """Turn the driver's JSON into a typed :class:`CTypeResolution`.

    A ``tool_error`` in the driver result means the binding could not be imported
    or libclang could not be initialised → :data:`TOOL_FAILED`. Structured
    ``issues`` (an incomplete build context) → :data:`BLOCKED`. Only a clean
    result is :data:`RESOLVED`.
    """
    version = str(raw.get("clang_version", ""))
    diagnostics = [str(d) for d in raw.get("diagnostics", [])]
    if raw.get("tool_error"):
        return CTypeResolution(
            status=TOOL_FAILED, clang_version=version, diagnostics=diagnostics,
            message=f"libclang could not be used: {raw['tool_error']}",
        )
    issues = list(raw.get("issues", []))
    if issues:
        summary = "; ".join(str(i.get("message", i.get("kind", ""))) for i in issues)
        return CTypeResolution(
            status=BLOCKED, clang_version=version, diagnostics=diagnostics,
            issues=issues,
            message=f"types could not be resolved from the selected build context: {summary}",
        )
    functions = [_function_from_json(f) for f in raw.get("functions", [])]
    return CTypeResolution(status=RESOLVED, clang_version=version,
                           functions=functions, diagnostics=diagnostics, issues=[])


def _function_from_json(data: dict[str, Any]) -> FunctionFact:
    return FunctionFact(
        name=str(data.get("name", "")),
        file=str(data.get("file", "")),
        line=int(data.get("line", 0)),
        variadic=bool(data.get("variadic", False)),
        return_type=_type_from_json(data.get("return", {})),
        params=[_type_from_json(p) for p in data.get("params", [])],
    )


def _type_from_json(data: dict[str, Any]) -> TypeFact:
    return TypeFact(
        spelling=str(data.get("spelling", "")),
        canonical=str(data.get("canonical", "")),
        kind=str(data.get("kind", "")),
        base=str(data.get("base", "")),
        depth=int(data.get("depth", 0)),
        qualifiers=list(data.get("qualifiers", [])),
        record=data.get("record"),
        opaque=bool(data.get("opaque", False)),
        aliases=[
            AliasFact(name=str(a.get("name", "")), file=str(a.get("file", "")),
                      line=int(a.get("line", 0)),
                      inline_body=bool(a.get("inline_body", False)),
                      opaque=bool(a.get("opaque", False)))
            for a in data.get("aliases", [])
        ],
    )


class CTypeResolutionError(RuntimeError):
    """A transport-level failure of the libclang driver (→ ``TOOL_FAILED``)."""


def _image_resolver(spec: dict[str, Any], context: ResolveContext) -> dict[str, Any]:
    """Run the in-image driver and return its JSON result.

    Uses the same ``docker run`` convention as :mod:`mtm.ceedling`: the bench is
    mounted **read-only** at :data:`CONTAINER_BENCH` (the driver writes nothing
    into it), ``HOME`` is redirected to ``/tmp`` and the package is importable at
    ``/opt/mtm``. A ``docker`` that cannot be launched or a container that produced
    no JSON is a :class:`CTypeResolutionError` (→ :data:`TOOL_FAILED`).
    """
    argv = [
        "docker", "run", "--rm", "-i",
        "--user", context.user or "0:0",
        "-e", "HOME=/tmp",
        "-e", f"PYTHONPATH={CONTAINER_PYTHONPATH}",
        "-v", f"{context.bench_root}:{CONTAINER_BENCH}:ro",
        "-w", CONTAINER_BENCH,
        "--entrypoint", "python3",
        context.image_ref, *DRIVER_ARGV,
    ]
    try:
        proc = subprocess.run(argv, input=json.dumps(spec), capture_output=True,
                              text=True, timeout=context.timeout, check=False)
    except subprocess.TimeoutExpired as exc:
        raise CTypeResolutionError(
            f"the libclang driver timed out after {context.timeout}s") from exc
    except OSError as exc:
        raise CTypeResolutionError(
            f"could not launch 'docker' for the libclang driver: {exc}") from exc
    out = proc.stdout or ""
    try:
        return json.loads(out)
    except json.JSONDecodeError as exc:
        detail = (proc.stderr or out).strip()[-500:]
        raise CTypeResolutionError(
            f"the libclang driver returned no result (exit {proc.returncode}): {detail}"
        ) from exc


def _invoking_user() -> str | None:
    try:
        if hasattr(os, "getuid"):
            return f"{os.getuid()}:{os.getgid()}"
    except OSError:  # pragma: no cover
        pass
    return None


def _default_image() -> str:
    """The product image reference, imported lazily to avoid an import cycle."""
    from .docker import image_ref

    return image_ref()
