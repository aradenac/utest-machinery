"""Shared pytest fixtures: run the CLI in a temporary bench, fully offline."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]


SCHEMAS = REPO / "mtm" / "schemas"


def run_mtm(*args: str, cwd: Path | None = None, env: dict | None = None) -> subprocess.CompletedProcess:
    """Invoke the mtm CLI as a subprocess and capture its result."""
    base_env = {
        "PATH": "/usr/bin:/bin",
        "PYTHONPATH": str(REPO),
        "HOME": str(Path.home()),
    }
    if env:
        base_env.update(env)
    return subprocess.run(
        [sys.executable, "-m", "mtm.cli", *args],
        cwd=str(cwd or REPO),
        capture_output=True,
        text=True,
        env=base_env,
    )


def dagu_dir() -> str | None:
    """Directory holding a host ``dagu`` binary, or None if none is available."""
    import shutil

    found = shutil.which("dagu")
    return str(Path(found).parent) if found else None


def run_mtm_json(*args: str, cwd: Path | None = None, env: dict | None = None) -> tuple[dict, int]:
    proc = run_mtm("--json", *args, cwd=cwd, env=env)
    try:
        return json.loads(proc.stdout), proc.returncode
    except json.JSONDecodeError:
        raise AssertionError(f"non-JSON output: {proc.stdout!r} stderr={proc.stderr!r}")


@pytest.fixture
def bench(tmp_path: Path) -> Path:
    """An initialised bench in a temporary directory."""
    target = tmp_path / "bench"
    target.mkdir()
    record, code = run_mtm_json("init", str(target))
    assert code == 0, record
    return target


@pytest.fixture
def product_image() -> str:
    """The built product image ref, or skip the test when it is absent.

    Execution-tier PR 2 tests need the image (Ruby/Ceedling and the C toolchain
    live there). The fast CI matrix has no image, so these are skipped; the
    ``generate-smoke`` CI job builds it and runs the end-to-end path.
    """
    from mtm.docker import image_exists
    from mtm.docker import image_ref as _image_ref

    ref = _image_ref()
    if not image_exists(ref):
        pytest.skip(f"product image {ref} is not built (execution-tier test)")
    return ref


def chart_alias_statement(statement: str):
    """Cheap (base, depth, opaque, inline, record) for the offline test double.

    Recovers a pointer alias's depth and whether the defining statement has an
    inline ``{…}`` body; a struct/enum/array/function alias is marked record
    (aggregate) / opaque (unsupported). This approximates libclang's facts so the
    generator's clang-driven path is exercised without Docker — deliberately *not*
    a product type authority (the real adapter is exercised by the docker tier).
    """
    import re as _re
    body = _re.sub(r"^\s*typedef\b", "", statement).rstrip(";").strip()
    inline = "{" in statement
    if "[" in body or ("(" in body and "*" in body):
        return "", 0, True, inline, None
    if inline:
        m = _re.search(r"\b(struct|union|enum)\b", body)
        kind = {"struct": "RECORD", "union": "UNION", "enum": "ENUM"}.get(
            m.group(1) if m else "", "RECORD")
        return "", 0, False, True, {"kind": kind, "tag": ""}
    if _re.match(r"(struct|union|enum)\b", body):
        m = _re.match(r"(struct|union|enum)\b", body)
        kind = {"struct": "RECORD", "union": "UNION", "enum": "ENUM"}[m.group(1)]
        return body, 0, False, False, {"kind": kind, "tag": ""}
    segs = body.split("*")
    depth = len(segs) - 1
    head = segs[0].split()
    return " ".join(head).strip(), depth, False, False, None


def host_double_include_unsafe(files, include_dirs) -> dict:
    """Which input headers the **real host compiler** rejects twice in one TU.

    Mirrors the adapter driver's clang probe with gcc: a two-``#include`` one-line
    TU per header, pinned to ``-std=c99`` to match the target compiler's language
    mode (a repeated tag body is a redeclaration there); a redefinition/conflicting-
    types error marks it unsafe. A missing compiler yields an empty map, which only
    weakens the offline contrast, never the docker-tier proof.
    """
    import shutil
    import subprocess
    import tempfile
    gcc = shutil.which("gcc") or shutil.which("cc")
    if gcc is None:
        return {}
    incs = [str(Path(d)) for d in include_dirs]
    unsafe: dict = {}
    with tempfile.TemporaryDirectory() as tmp:
        for path in files:
            if not path.is_file() or path.suffix != ".h":
                continue
            probe = Path(tmp) / "probe.c"
            probe.write_text(f'#include "{path}"\n#include "{path}"\n')
            proc = subprocess.run(
                [gcc, "-xc", "-std=c99", "-fsyntax-only",
                 *[f"-I{i}" for i in incs], "-I", str(path.parent), str(probe)],
                capture_output=True, text=True)
            out = (proc.stderr or "") + (proc.stdout or "")
            unsafe[path.name] = proc.returncode != 0 and any(
                k in out for k in ("redefinition", "conflicting types",
                                   "redeclaration", "redefined", "previous definition"))
    return unsafe


def fake_resolve(paths, *, bench_root=None, include_dirs=(), defines=(), args=(),
                 image=None, user=None, timeout=300, resolver=None):
    """An offline clang-adapter stub shared by the generate/qualify/runner tests.

    Charts the input aliases into the adapter's ``CTypeResolution`` shape and asks
    the **real host compiler** whether each header may be included twice in one TU,
    so the generator's clang-driven, compiler-gated path is exercised without
    Docker. It never guesses a type — a *test double*, not the authority.
    """
    from mtm import ctypes_clang
    from mtm.cscan import typedef_statement_map

    files = [Path(p) for p in paths]
    for d in include_dirs:
        d = Path(d)
        if d.is_dir():
            files += [p for p in sorted(d.glob("*.h"))]
    aliases: dict = {}
    for path in sorted(files, key=lambda p: (p.suffix != ".h", str(p))):
        if not path.is_file():
            continue
        for alias, statement in typedef_statement_map(
                path.read_text(encoding="utf-8", errors="replace")).items():
            base, depth, opaque, inline, record = chart_alias_statement(statement)
            if alias in aliases and path.suffix != ".h":
                continue  # prefer the header defining location
            aliases[alias] = ctypes_clang.AliasFact(
                name=alias, file=path.name, line=1, base=base, depth=depth,
                record=record, inline_body=inline, opaque=opaque)
    return ctypes_clang.CTypeResolution(
        status="RESOLVED", clang_version="test-double", functions=[],
        aliases=aliases,
        double_include_unsafe=host_double_include_unsafe(files, include_dirs))
