"""PR 3 lot A: the minimal libclang semantic adapter (``mtm.ctypes_clang``).

Two tiers:

* **Offline (always runs).** The mapping from the in-image driver's JSON to the
  typed :class:`~mtm.ctypes_clang.CTypeResolution`, the BLOCKED vs TOOL_FAILED
  classification, and the resolvable shapes — exercised through the injectable
  ``resolver`` seam, so no Docker/clang is needed.
* **Execution tier (``docker`` marker).** The real adapter, running libclang-18
  inside the built product image, resolves a representative header and shows the
  exact F3 fact (``typedef int *P; P *pp`` → depth **2**).

The representative header carries the whole contrast family in one file:
``typedef int *P;`` used as ``P *pp`` (depth 2), an anonymous record ``S`` and
enum ``e_t`` (inline-body aliases), a tag alias ``T`` (no inline body), a
function-pointer alias ``CB`` (opaque), and a plain ``const unsigned long *``.
"""

from __future__ import annotations

import json

import pytest

from mtm import ctypes_clang as ct
from mtm.ctypes_clang import (
    BLOCKED,
    RESOLVED,
    TOOL_FAILED,
    AliasFact,
    CTypeResolution,
    FunctionFact,
    ResolveContext,
    TypeFact,
)

#: The representative header used by every test here.
REPRESENTATIVE = """\
typedef int *P;
typedef struct { int v; } S;
typedef enum { A, B } e_t;
struct Tag { int w; };
typedef struct Tag T;
typedef void (*CB)(int);
int dep_write(P *pp, S s, e_t e, T t, CB cb, const unsigned long *q, int n);
"""

#: The driver JSON the real image returns for ``REPRESENTATIVE`` (captured from a
#: real libclang-18 run; kept here as the offline mapping oracle).
DRIVER_JSON = {
    "ok": True,
    "clang_version": "Debian clang version 18.1.8",
    "diagnostics": [],
    "issues": [],
    "functions": [
        {
            "name": "dep_write", "file": "repr.h", "line": 7, "variadic": False,
            "return": {"spelling": "int", "canonical": "int", "kind": "INT",
                       "base": "int", "depth": 0, "qualifiers": [],
                       "record": None, "opaque": False, "aliases": []},
            "params": [
                {"spelling": "P *", "canonical": "int **", "kind": "POINTER",
                 "base": "int", "depth": 2, "qualifiers": [], "record": None,
                 "opaque": False,
                 "aliases": [{"name": "P", "file": "repr.h", "line": 1,
                              "inline_body": False, "opaque": False}]},
                {"spelling": "S", "canonical": "S", "kind": "RECORD", "base": "S",
                 "depth": 0, "qualifiers": [],
                 "record": {"kind": "RECORD", "tag": "S"}, "opaque": False,
                 "aliases": [{"name": "S", "file": "repr.h", "line": 2,
                              "inline_body": True, "opaque": False}]},
                {"spelling": "e_t", "canonical": "e_t", "kind": "ENUM", "base": "e_t",
                 "depth": 0, "qualifiers": [],
                 "record": {"kind": "ENUM", "tag": "e_t"}, "opaque": False,
                 "aliases": [{"name": "e_t", "file": "repr.h", "line": 3,
                              "inline_body": True, "opaque": False}]},
                {"spelling": "T", "canonical": "struct Tag", "kind": "RECORD",
                 "base": "struct Tag", "depth": 0, "qualifiers": [],
                 "record": {"kind": "RECORD", "tag": "Tag"}, "opaque": False,
                 "aliases": [{"name": "T", "file": "repr.h", "line": 5,
                              "inline_body": False, "opaque": False}]},
                {"spelling": "CB", "canonical": "void (*)(int)", "kind": "POINTER",
                 "base": "void (int)", "depth": 1, "qualifiers": [], "record": None,
                 "opaque": True,
                 "aliases": [{"name": "CB", "file": "repr.h", "line": 6,
                              "inline_body": False, "opaque": True}]},
                {"spelling": "const unsigned long *",
                 "canonical": "const unsigned long *", "kind": "POINTER",
                 "base": "const unsigned long", "depth": 1,
                 "qualifiers": ["const"], "record": None, "opaque": False,
                 "aliases": []},
                {"spelling": "int", "canonical": "int", "kind": "INT", "base": "int",
                 "depth": 0, "qualifiers": [], "record": None, "opaque": False,
                 "aliases": []},
            ],
        }
    ],
}


def _stub(payload):
    """A resolver returning *payload*, recording the spec it was handed."""
    seen: dict = {}

    def resolver(spec, context):
        seen["spec"] = spec
        seen["context"] = context
        return payload

    return resolver, seen


# --- offline: mapping -------------------------------------------------------


def test_resolve_maps_the_representative_header_to_typed_facts(tmp_path):
    """The driver JSON becomes typed facts with the expected depths/aliases."""
    (tmp_path / "repr.h").write_text(REPRESENTATIVE)
    resolver, _ = _stub(DRIVER_JSON)
    result = ct.resolve([tmp_path / "repr.h"], bench_root=tmp_path,
                        image="utest-machinery/mtm:1.0.0", resolver=resolver)

    assert result.status == RESOLVED and result.ok
    fn = result.by_name("dep_write")
    assert isinstance(fn, FunctionFact)
    assert fn.variadic is False
    assert isinstance(fn.return_type, TypeFact) and fn.return_type.depth == 0

    pp, s, e, t, cb, q, n = fn.params
    # F3: `typedef int *P` used as `P *pp` is `int **` — depth 2, not 1.
    assert pp.depth == 2 and pp.canonical == "int **" and pp.base == "int"
    assert pp.aliases[0] == AliasFact("P", "repr.h", 1, inline_body=False, opaque=False)
    # Anonymous record / enum: identity by tag, inline-body alias.
    assert s.record == {"kind": "RECORD", "tag": "S"} and s.aliases[0].inline_body
    assert e.record == {"kind": "ENUM", "tag": "e_t"} and e.aliases[0].inline_body
    # Tag alias: record identity from the tag, no inline body on the alias stmt.
    assert t.record == {"kind": "RECORD", "tag": "Tag"} and not t.aliases[0].inline_body
    # Function-pointer alias: flagged opaque (not renderable as a scalar).
    assert cb.opaque and cb.aliases[0].opaque
    # Plain pointer: qualifiers preserved, depth 1, no alias.
    assert q.depth == 1 and q.qualifiers == ["const"] and q.aliases == []
    assert n.depth == 0 and n.base == "int"


def test_resolve_mounts_the_bench_read_only_and_maps_container_paths(tmp_path):
    """The request mounts the bench read-only; the driver sees container paths."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "repr.h").write_text(REPRESENTATIVE)
    resolver, seen = _stub(DRIVER_JSON)
    ct.resolve(["src/repr.h"], include_dirs=["src"], defines=["FOO=1"],
               bench_root=tmp_path, image="img", user="1000:1000",
               resolver=resolver)
    spec = seen["spec"]
    assert spec["root"] == "/bench"
    assert spec["inputs"] == ["/bench/src/repr.h"]
    assert spec["include_dirs"] == ["/bench/src"]
    assert spec["defines"] == ["FOO=1"]
    assert seen["context"].bench_root == tmp_path.resolve()


def test_to_dict_round_trips_the_fact_set(tmp_path):
    (tmp_path / "repr.h").write_text(REPRESENTATIVE)
    resolver, _ = _stub(DRIVER_JSON)
    result = ct.resolve([tmp_path / "repr.h"], bench_root=tmp_path,
                        image="img", resolver=resolver)
    data = result.to_dict()
    assert data["status"] == RESOLVED
    assert data["functions"][0]["params"][0]["depth"] == 2
    json.dumps(data)  # the fact set must be JSON-serializable (evidence/provenance)


# --- offline: failure classification ----------------------------------------


def test_missing_build_context_is_blocked_with_a_diagnostic(tmp_path):
    """A missing header → BLOCKED (input limitation), never a guessed type."""
    payload = {
        "ok": False, "clang_version": "18.1.8",
        "diagnostics": ["/bench/repr.h:1:10: Fatal: 'vendor.h' file not found"],
        "issues": [{"kind": ct.MISSING_CONTEXT,
                    "message": "'vendor.h' file not found",
                    "file": "/bench/repr.h", "line": 1}],
        "functions": [{"name": "dep_write", "file": "repr.h", "line": 7,
                       "variadic": False, "return": {}, "params": []}],
    }
    resolver, _ = _stub(payload)
    result = ct.resolve([tmp_path / "repr.h"], bench_root=tmp_path,
                        image="img", resolver=resolver)
    assert result.status == BLOCKED and not result.ok
    assert result.functions == []  # a blocked result never carries a declaration
    assert "vendor.h" in result.message
    assert "file not found" in result.diagnostics[0]


def test_broken_binding_is_tool_failed(tmp_path):
    """A driver that could not import libclang → TOOL_FAILED, not BLOCKED."""
    resolver, _ = _stub({"ok": False, "tool_error": "ModuleNotFoundError: clang"})
    result = ct.resolve([tmp_path / "repr.h"], bench_root=tmp_path,
                        image="img", resolver=resolver)
    assert result.status == TOOL_FAILED
    assert "clang" in result.message


def test_a_docker_launch_failure_is_tool_failed(tmp_path):
    """`docker` absent on the host → TOOL_FAILED with an actionable message."""

    def resolver(spec, context):
        raise ct.CTypeResolutionError("could not launch 'docker': not found")

    result = ct.resolve([tmp_path / "repr.h"], bench_root=tmp_path,
                        image="img", resolver=resolver)
    assert result.status == TOOL_FAILED and "docker" in result.message


def test_unparsable_docker_output_is_tool_failed(tmp_path, monkeypatch):
    """A container that printed no JSON → TOOL_FAILED (never a silent empty pass)."""
    class _Proc:
        returncode = 1
        stdout = "Traceback (most recent call last): ..."
        stderr = "boom"

    monkeypatch.setattr(ct.subprocess, "run",
                        lambda *a, **k: _Proc())
    context = ResolveContext(bench_root=tmp_path, image_ref="img")
    with pytest.raises(ct.CTypeResolutionError):
        ct._image_resolver({"inputs": []}, context)


def test_image_resolver_argv_mounts_the_bench_readonly(tmp_path, monkeypatch):
    """The default transport mounts the bench read-only and runs the driver module."""
    captured: dict = {}

    class _Proc:
        returncode = 0
        stdout = json.dumps(DRIVER_JSON)
        stderr = ""

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        captured["input"] = kwargs.get("input")
        return _Proc()

    monkeypatch.setattr(ct.subprocess, "run", fake_run)
    context = ResolveContext(bench_root=tmp_path.resolve(), image_ref="img",
                             user="1000:1000")
    out = ct._image_resolver({"inputs": ["/bench/x.h"]}, context)
    assert out["functions"][0]["name"] == "dep_write"
    argv = captured["argv"]
    assert argv[:3] == ["docker", "run", "--rm"]
    assert f"{tmp_path.resolve()}:/bench:ro" in argv
    assert "--entrypoint" in argv and argv[argv.index("--entrypoint") + 1] == "python3"
    assert argv[-2:] == ["-m", "mtm.clang_resolve_driver"]
    assert json.loads(captured["input"])["inputs"] == ["/bench/x.h"]


# --- execution tier: the real adapter in the product image ------------------


@pytest.mark.docker
def test_real_image_resolves_the_representative_header(tmp_path, product_image):
    """DoD: libclang-18 in the image resolves the header — F3 depth is 2.

    Raw evidence is kept (a real parse under the effective build context), the
    version is a real 18.x binding, and a missing-include request is BLOCKED.
    """
    (tmp_path / "repr.h").write_text(REPRESENTATIVE)
    result = ct.resolve([tmp_path / "repr.h"], bench_root=tmp_path,
                        image=product_image)
    assert result.status == RESOLVED, (result.status, result.message, result.diagnostics)
    assert "18.1" in result.clang_version, result.clang_version
    fn = result.by_name("dep_write")
    assert fn is not None

    pp = fn.params[0]
    assert pp.depth == 2, (pp.spelling, pp.canonical)  # the F3 defect, resolved
    assert pp.canonical == "int **"
    assert pp.aliases[0].name == "P" and pp.aliases[0].line == 1
    assert not pp.aliases[0].inline_body

    s, e, t, cb = fn.params[1], fn.params[2], fn.params[3], fn.params[4]
    assert s.record == {"kind": "RECORD", "tag": "S"} and s.aliases[0].inline_body
    assert e.record == {"kind": "ENUM", "tag": "e_t"} and e.aliases[0].inline_body
    assert t.record == {"kind": "RECORD", "tag": "Tag"}
    assert cb.opaque

    # A request whose header is missing is BLOCKED with a real diagnostic.
    (tmp_path / "needs.h").write_text('#include "absent_vendor.h"\nint f(void);\n')
    blocked = ct.resolve([tmp_path / "needs.h"], bench_root=tmp_path,
                         image=product_image)
    assert blocked.status == BLOCKED
    assert any("not found" in d for d in blocked.diagnostics), blocked.diagnostics
