"""Deterministic Ceedling/CMock project generation from an accepted plan.

Given a grounded :class:`~mtm.plan.Plan`, this module emits a **standard
Ceedling project** — ``project.yml``, the in-scope sources, a private interface
header per mocked dependency, the plan's private shims and one Unity test file
per unit — purely by rendering the plan. Ceedling then generates the CMock mocks
and the runner, compiles, links and executes. The model never writes C here, so
it cannot weaken an oracle between the reviewed plan and the executed assertion
(``automation-policy.md``).

Everything is written under ``<bench>/.mtm/gen`` (a fresh revision directory,
rebuilt on every run), never into the selected inputs: the original C/SDD bytes
are untouched (SYS-004). A symbol or case the generator cannot materialise
faithfully is reported as a limitation, never silently stubbed into a passing
test.
"""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .ceedling import REVISION_DIRNAME
from .inventory import Inventory, resolve_scope_paths
from .plan import Plan, Scenario

_VOID = {"void", ""}
_CTYPE_WORDS = (
    "unsigned", "signed", "long", "short", "int", "char", "float", "double",
    "bool", "_Bool",
)

#: Where the mocked dependencies' interface headers live, inside the revision.
INTERFACE_DIRNAME = "interface"
#: Where the plan's private shims live.
SHIM_DIRNAME = "shim"
#: Where the copied in-scope sources and headers live.
SOURCE_DIRNAME = "src"
#: Where the generated Unity test files live (Ceedling ``:paths: :test``).
TEST_DIRNAME = "test"


@dataclass
class CParam:
    """A parsed C parameter.

    ``type`` is the parameter's **base (pointee) type** with qualifiers preserved
    (``const char``, ``void``, ``unsigned long``); ``depth`` is its number of ``*``
    indirection levels (0 = passed by value). The full declarator is
    ``type`` + ``depth`` stars. Keeping the depth — rather than a single
    ``pointer`` boolean — is what lets the emitter pass an ``int **`` where an
    ``int **`` is declared, instead of an ``int *``.
    """

    type: str
    name: str
    depth: int = 0

    @property
    def pointer(self) -> bool:
        return self.depth > 0


@dataclass
class Generated:
    """The materialised Ceedling project manifest."""

    revision: Path | None = None
    files: dict[str, str] = field(default_factory=dict)  # relpath -> role
    test_functions: list[str] = field(default_factory=list)
    mocks: list[str] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "revision": str(self.revision) if self.revision else "",
            "files": self.files,
            "test_functions": self.test_functions,
            "mocks": self.mocks,
            "limitations": self.limitations,
        }


# --- C signature parsing ----------------------------------------------------


def split_params(params: str) -> list[CParam]:
    """Split a C parameter list into ``CParam``s by top-level commas."""
    params = params.strip()
    if params in ("", "void"):
        return []
    result: list[CParam] = []
    depth = 0
    current = ""
    for ch in params:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            result.append(parse_param(current))
            current = ""
        else:
            current += ch
    result.append(parse_param(current))
    return result


def parse_param(text: str) -> CParam:
    """Parse one C parameter declarator into a :class:`CParam`.

    The pointer *depth* is preserved (``int **pp`` → depth 2) and the **pointee**
    qualifiers are kept (``const char *s`` → type ``const char``), so the emitter
    can reconstruct a type-correct declarator. Only qualifiers in the base segment
    are kept: a qualifier on a pointer *level* (``int * const p``, ``char * const
    *p``) is ignored by C for compatibility and does **not** belong to the pointee,
    so keeping it in the base would change the type (``char * const *`` would
    become the incompatible ``const char **``).
    """
    text = text.strip()
    segments = text.split("*")
    depth = len(segments) - 1
    if depth == 0:
        tokens = text.split()
        if not tokens:
            return CParam(type="", name="", depth=0)
        if len(tokens) >= 2 and tokens[-1] not in _CTYPE_WORDS \
                and not _looks_like_type(tokens[-1]):
            return CParam(type=" ".join(tokens[:-1]).strip(), name=tokens[-1], depth=0)
        if len(tokens) == 1:
            return CParam(type=tokens[0], name="", depth=0)
        return CParam(type=text, name="", depth=0)
    # depth >= 1: the pointee type is the first segment (qualifiers kept, storage
    # class dropped); the declared name is the trailing token of the last segment.
    base_tokens = [t for t in segments[0].split() if t not in _STORAGE_CLASS]
    tail = segments[-1].split()
    name = tail[-1] if tail else ""
    return CParam(type=" ".join(base_tokens).strip(), name=name, depth=depth)


def _looks_like_type(token: str) -> bool:
    return token.endswith("_t") or token in _CTYPE_WORDS


def _unsupported_param_shape(params: list[CParam]) -> str | None:
    """A reason a parameter list cannot be materialised faithfully, or ``None``.

    The generator renders each argument as a cast to the parameter's type and,
    for a pointer parameter, a declared local initialised from a scalar. A
    parameter that this generator cannot render faithfully — a variadic ``...``,
    or a function-pointer or array parameter — would emit a call that does not
    compile (an opaque ``TOOL_FAILURE``) or passes a scalar where the callee
    dereferences it (a run-time crash misread as an ``IMPLEMENTATION_DEFECT``).
    Report it so the caller records a limitation and refuses the scenario, never
    an admitted-but-uncompilable/crashing bench (``implementation.md``:
    "unsupported shapes are recorded limitations, never silent passes").

    An **unnamed** parameter (``int foo(int)``, ``int foo(char *)``) is *not*
    refused: that is legal C and renders fine as ``(type)value`` — refusing it
    would be a false refusal of a common prototype form.
    """
    for param in params:
        if "..." in param.type:
            return "a variadic (`...`) parameter is not supported"
        if any(ch in param.type or ch in param.name for ch in "(["):
            # The scanner leaves a function-pointer/array suffix in the parsed
            # type or name (`int a[3]` → name `a[3]`; `int (*cb)(int)` → name
            # `(cb)(int)`); the generator cannot render such an argument.
            return ("a function-pointer or array parameter is not supported")
    return None


def func_params(signature: str) -> list[CParam]:
    m = re.search(r"\(([^)]*)\)", signature)
    return split_params(m.group(1)) if m else []


def return_type(signature: str) -> str:
    """Return type of a C signature string (the tokens before the name)."""
    head = signature.split("(", 1)[0].strip()
    parts = head.split()
    return " ".join(parts[:-1]) if len(parts) > 1 else ""


def return_is_void(signature: str) -> bool:
    return return_type(signature).strip() in _VOID


def _unit_includes(unit, scenarios: list[Scenario] | None, copied: set[str] | None,
                   src_dir: Path | None, iface_dir: Path, out: "Generated") -> list[str] | None:
    """The include(s) a test file must use for *unit*, in order, or ``None`` to skip.

    Ceedling links ``<stem>.c`` into a test executable **only when the test
    includes ``<stem>.h``**. So the test must always end up including a header
    named after the source stem; a test that includes only a *differently named*
    header that happens to declare the SUT (``sensor.c`` includes ``api.h``) still
    compiles but never links ``sensor.c`` — an opaque ``undefined reference``. The
    resolution is:

    * a real copied ``<stem>.h`` that declares **every** function under test →
      include it alone;
    * a real copied ``<stem>.h`` that declares only *some* of them → include it
      (fidelity) **and** a private helper header that declares the remaining ones,
      so the test never calls a function with no visible prototype;
    * a real copied ``<stem>.h`` that declares **none** of them → refuse
      (``None``): the private stem header would need the same name and cannot be
      added without shadowing the selected one;
    * otherwise → synthesise a private ``<stem>.h`` from the scanner's prototypes
      (so the SUT links) and, for fidelity, also include any real header that
      declares it. Returns ``None`` (with a recorded limitation) when no
      declarations can be produced.
    """
    stem = Path(unit.path).stem
    if not stem:
        return []  # no source file to link; the caller emits a declaration-only test
    stem_header = f"{stem}.h"
    declaring = _declaring_header(unit, scenarios, copied, src_dir)
    if copied is not None and stem_header in copied:
        # A selected `<stem>.h` exists. It either declares the functions under test
        # (use it) or it does not — then we cannot add our own same-named header
        # without shadowing the selected one, so refuse rather than emit a
        # mislinked test.
        covered = _functions_declared_by(unit, scenarios, copied, src_dir, stem_header)
        if declaring == stem_header:
            # A stem header that declares a function under test with an
            # *incompatible* signature (different arity/parameter types) cannot be
            # worked around: the test would see the header's prototype and call it
            # with the plan's arguments — a hard compile error — and a private
            # declaration would clash with the header. Refuse rather than emit
            # uncompilable code (the class E2 exists to remove).
            conflicts = _conflicting_declarations(unit, scenarios, copied, src_dir,
                                                   stem_header)
            if conflicts:
                out.limitations.append(
                    f"unit {unit.id} ({unit.path}) cannot be linked: the selected "
                    f"{stem_header!r} declares {sorted(conflicts)!r} with a signature "
                    "incompatible with the source; the test could not call it")
                return None
            missing = _wanted_functions(scenarios) - covered
            if not missing:
                return [stem_header]
            # The stem header declares only *part* of the unit's functions under
            # test (e.g. `foo.h` declares `foo_a` but not `foo_b`). The test would
            # call the missing functions with no visible prototype; add a private
            # helper header (a different name, so the selected file is not
            # shadowed) declaring exactly those, grounded in the scanner.
            helper_name = f"mtm_unit_{stem}_extra.h"
            if copied is not None and helper_name in copied:
                # A selected file already has the helper's name: writing our
                # interface header would shadow it (interface/ precedes src/ on the
                # include path), exactly the shadowing this rule exists to avoid.
                out.limitations.append(
                    f"unit {unit.id} ({unit.path}) cannot be linked: a selected "
                    f"{helper_name!r} exists and cannot be shadowed by the private "
                    "helper header")
                return None
            helper = _synthesise_unit_header(unit, scenarios, iface_dir, out,
                                             header_name=helper_name,
                                             only=sorted(missing))
            if not helper:
                return None
            return [stem_header, helper]
        out.limitations.append(
            f"unit {unit.id} ({unit.path}) cannot be linked: a selected "
            f"{stem_header!r} exists but does not declare its functions under test")
        return None
    include_names: list[str] = []
    if declaring:
        # The real declaring header (a non-stem one here) is included for fidelity;
        # if it declares a function under test incompatibly, the synthesised stem
        # header would clash with it. Refuse rather than emit a conflicting bench.
        conflicts = _conflicting_declarations(unit, scenarios, copied, src_dir,
                                              declaring)
        if conflicts:
            out.limitations.append(
                f"unit {unit.id} ({unit.path}) cannot be linked: the selected "
                f"{declaring!r} declares {sorted(conflicts)!r} with a signature "
                "incompatible with the source; the test could not call it")
            return None
        include_names.append(declaring)
    synth = _synthesise_unit_header(unit, scenarios, iface_dir, out)
    if not synth:
        return None
    if synth not in include_names:
        include_names.append(synth)
    return include_names


def _wanted_functions(scenarios: list[Scenario] | None) -> set[str]:
    """The functions under test named by *scenarios*."""
    return {sc.function for sc in (scenarios or [])}


def _functions_declared_by(unit, scenarios: list[Scenario] | None,
                           copied: set[str] | None, src_dir: Path | None,
                           header: str) -> set[str]:
    """Functions under test that a copied local *header* actually declares."""
    if copied is not None and header not in copied:
        return set()
    path = (src_dir / header) if src_dir is not None else None
    if path is None or not path.is_file():
        return set()
    from .cscan import scan_prototypes

    declared = {n for n, _ in scan_prototypes(path)}
    return declared & _wanted_functions(scenarios)


#: C type qualifiers and storage-class specifiers that never change a function's
#: *type* in the positions this key compares. Storage class (`extern`, `static`,
#: `inline`, …) is dropped everywhere; top-level qualifiers (`const`, `volatile`,
#: `restrict`) are dropped only on a **non-pointer** type, where they are ignored
#: for compatibility. On a pointer type the qualifier is part of the type
#: (`char *` and `const char *` are different types — gcc: "conflicting types"),
#: so it must be kept or a genuine conflict would be admitted.
_STORAGE_CLASS = {"extern", "static", "inline", "register", "auto", "typedef",
                  "_Noreturn"}
_QUALIFIERS = {"const", "volatile", "restrict"}


def _type_key(raw: str, name: str = "", *, drop_top: bool = True) -> str:
    """A comparison key for a raw C type/declarator, one ``|``-joined segment per
    ``*`` level (so pointer **depth** is preserved).

    C's rule (6.7.6.3p15): for function-type compatibility a **parameter** is taken
    as having the *unqualified* version of its outermost type — so a top-level
    qualifier on a parameter or on the pointer it is (``const int x`` == ``int x``,
    ``int * const p`` == ``int *p``) is ignored. Every *other* level keeps its
    qualifier **by identity and position**: an inner-pointer or pointee qualifier
    is part of the type (``const char *`` != ``char *``; ``char * const *`` !=
    ``char **``; ``const int *`` != ``volatile int *``) — dropping or collapsing
    those admits a real conflict that then dies as an opaque build failure.

    Within a segment, storage class is dropped, the base tokens are compared
    order-insensitively (``unsigned long`` == ``long unsigned``) and the qualifier
    set is sorted (``const volatile`` == ``volatile const``). The declared *name*
    is removed. A return type uses ``drop_top=False`` (no parameter adjustment).

    A deliberate textual approximation, **not** a full C type system: a
    typedef-alias spelling of the same type (`size_t` vs `unsigned long`) is still
    treated as a difference.
    """
    text = _strip_c_comments(raw or "").strip().rstrip(";")
    if name:
        text = re.sub(rf"\b{re.escape(name)}\s*$", "", text).strip()
    segments = text.split("*")
    keys: list[str] = []
    for index, segment in enumerate(segments):
        tokens = [t for t in re.split(r"\s+", segment.strip())
                  if t and t not in _STORAGE_CLASS]
        quals = sorted(t for t in tokens if t in _QUALIFIERS)
        base = sorted(t for t in tokens if t not in _QUALIFIERS)
        if drop_top and index == len(segments) - 1:
            quals = []  # the parameter's outermost qualifier is ignored by C
        keys.append("+".join(quals) + "/" + "+".join(base))
    return "|".join(keys)


def _raw_params(params: str) -> list[str]:
    """Split a raw C parameter list into per-parameter declarator strings.

    Keeps the ``*`` position (unlike ``split_params``/``parse_param``, which
    reduce a pointer to a bool and a stripped type).
    """
    params = params.strip()
    if params in ("", "void"):
        return []
    out: list[str] = []
    depth = 0
    current = ""
    for ch in params:
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        if ch == "," and depth == 0:
            out.append(current.strip())
            current = ""
        else:
            current += ch
    out.append(current.strip())
    return out


def _signature_key(signature: str) -> tuple | None:
    """A comparable key for a C signature, or ``None`` when unparseable.

    Parameter names are dropped and types normalised by :func:`_type_key`, so a
    header repeating the same declaration with different parameter names, a
    spurious outermost `const`, or an `extern` storage class is not a conflict,
    while a genuine type difference (arity, any non-outermost qualifier, pointer
    depth, base type, return type) still is. Parsed independently of
    ``split_declaration`` (which deliberately refuses *pointer returns* and other
    shapes the **generator** cannot emit): a pointer-returning prototype is a
    perfectly comparable declaration, and treating it as unparseable would silently
    skip the conflict check exactly where it is needed.
    """
    decl = _strip_c_comments(signature or "").strip().rstrip(";")
    if not decl:
        return None
    m = re.match(r"^(?P<ret>.+?)\b(?P<name>[A-Za-z_]\w*)\s*\((?P<params>.*)\)\s*$", decl)
    if not m or "..." in m.group("params"):
        return None
    ret_key = _type_key(m.group("ret"), drop_top=False)
    keys: list[str] = []
    for raw in _raw_params(m.group("params")):
        if "(" in raw or "[" in raw:
            return None  # function-pointer/array parameter: not comparable here
        keys.append(_type_key(raw, parse_param(raw).name))
    return (ret_key, tuple(keys))


def _conflicting_declarations(unit, scenarios: list[Scenario] | None,
                              copied: set[str] | None, src_dir: Path | None,
                              header: str) -> set[str]:
    """Functions under test the copied *header* declares incompatibly.

    Compares each header prototype against the unit's own scanned signature (the
    same source the generator uses for arity checks). An unparseable signature on
    either side is not treated as a conflict — only a definite disagreement.
    """
    if copied is not None and header not in copied:
        return set()
    path = (src_dir / header) if src_dir is not None else None
    if path is None or not path.is_file():
        return set()
    from .cscan import scan_prototypes

    own = {fn["name"]: fn.get("signature", "")
           for fn in getattr(unit, "functions", []) or []}
    wanted = _wanted_functions(scenarios)
    conflicts: set[str] = set()
    for name, decl in scan_prototypes(path):
        if name not in wanted or name not in own:
            continue
        header_key = _signature_key(decl)
        own_key = _signature_key(own[name])
        if header_key is not None and own_key is not None and header_key != own_key:
            conflicts.add(name)
    return conflicts


def _declaring_header(unit, scenarios: list[Scenario] | None,
                      copied: set[str] | None, src_dir: Path | None) -> str:
    """The best copied header that declares a function under test, or ``""``.

    Prefers the same-stem header (``foo.c`` → ``foo.h``); otherwise a local ``.h``
    the source already includes that declares one of the unit's functions under
    test. A ``.h`` that declares only unrelated symbols is never chosen.
    """
    from .cscan import scan_prototypes

    wanted = {sc.function for sc in (scenarios or [])}
    candidates: list[str] = []
    stem = Path(unit.path).stem
    if stem:
        candidates.append(f"{stem}.h")
    for inc in getattr(unit, "includes_local", []) or []:
        if inc.endswith(".h") and inc not in candidates:
            candidates.append(inc)
    for name in candidates:
        if copied is not None and name not in copied:
            continue
        if not wanted:
            return name
        path = (src_dir / name) if src_dir is not None else None
        if path is not None and path.is_file():
            declared = {n for n, _ in scan_prototypes(path)}
            if declared & wanted:
                return name
    return ""


# --- declarations -----------------------------------------------------------


def _strip_c_comments(text: str) -> str:
    """Remove C comments from model-supplied C text (an untrusted declaration).

    A ``mock_declarations`` entry legitimately explains its provenance in a
    comment, but a comment inside a declaration would break parsing and, worse,
    would be emitted verbatim into generated C. Strip it here so only the
    declaration itself survives.
    """
    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.DOTALL)
    return re.sub(r"//[^\n]*", " ", text)


def split_declaration(decl: str) -> tuple[str, list[CParam]] | None:
    """Split a C function declaration into ``(return_type, params)``.

    ``None`` when the declaration has a shape this generator does not support
    (variadics, a function-pointer/array parameter, or an unparseable header),
    so the caller records a limitation instead of emitting wrong code.
    """
    decl = _strip_c_comments(decl or "").strip().rstrip(";")
    if not decl:
        return None
    m = re.match(r"^(?P<ret>.+?)\b(?P<name>[A-Za-z_]\w*)\s*\((?P<params>.*)\)\s*$", decl)
    if not m:
        return None
    ret = m.group("ret").strip()
    if "*" in ret or "..." in m.group("params"):
        return None
    params = split_params(m.group("params"))
    for p in params:
        if not p.name or "(" in p.type:
            return None
    return ret, params


_AGGREGATE_STRUCT_RE = re.compile(
    r"typedef\s+struct\s+\w*\s*\{[^}]*\}\s*([A-Za-z_]\w*)\s*;", re.DOTALL)
_AGGREGATE_UNION_RE = re.compile(
    r"typedef\s+union\s+\w*\s*\{[^}]*\}\s*([A-Za-z_]\w*)\s*;", re.DOTALL)
_AGGREGATE_ARRAY_RE = re.compile(
    r"typedef\s+[^;{}]*\[\s*[^\]]*\]\s*([A-Za-z_]\w*)\s*;")


def aggregate_aliases(text: str) -> set[str]:
    """Type names in *text* that denote an aggregate (struct/union/array) type.

    A pointer to an aggregate cannot be initialised or set from a plain scalar,
    so a plan that supplies a scalar for such a pointee cannot be materialised
    faithfully. Detecting the aliases lets the generator refuse that case with a
    recorded limitation instead of emitting C that does not compile.
    """
    aliases: set[str] = set()
    for pattern in (_AGGREGATE_STRUCT_RE, _AGGREGATE_UNION_RE, _AGGREGATE_ARRAY_RE):
        aliases |= {m.group(1) for m in pattern.finditer(text)}
    return aliases


def _base_type(param: CParam) -> str:
    return param.type.strip().rstrip("*").strip()


def _cast(param: CParam, value: Any) -> str:
    return f"({_base_type(param)}){_expression(value)}"


def _pointer_arg(param: CParam, var: str, value: Any) -> tuple[list[str], str, str]:
    """Backing storage, call argument and post-call target for a pointer parameter.

    A plan supplies the **pointed-to scalar value** (``args``/``expect_out``
    semantics), so the emitter must build a correctly typed object and pass a
    pointer to it at the parameter's **exact declared indirection depth** —
    ``int **`` must receive an ``int **``, never an ``int *`` (the F1 defect), and
    ``void *`` must not emit the invalid ``void x = …``. Returns
    ``(lines, argument_expression, target_variable)`` where *target_variable* is the
    scalar local an ``expect_out`` assertion reads after the call.

    Depth 1 (the common case) is byte-identical to the original emission
    ``{base} {var} = ({base})value;`` / ``&{var}``. Depth d ≥ 2 declares the scalar
    and one pointer local per intermediate level, and passes the **address of the
    last** one so the argument has exactly d stars::

        T  v0 = value;        // pointee scalar
        T *p1 = &v0;          // one level up
        foo(&p1);             // T ** argument  (d == 2)

    A ``void`` pointee has no nameable object, so it is backed by the ``int`` scalar
    default and the resulting address is cast to the declared ``void *``-level type.
    """
    base = _base_type(param)
    if param.depth == 1 and base != "void":
        return ([f"    {base} {var} = {_cast(param, value)};"], f"&{var}", var)
    storage = "int" if base == "void" else base
    lines = [f"    {storage} {var}_v0 = ({storage}){_expression(value)};"]
    target = f"{var}_v0"
    if param.depth == 1:  # void *: address of the int-shaped backing, cast
        return (lines, f"(void *)&{target}", target)
    prev = f"&{target}"
    for level in range(1, param.depth):
        ptr_type = storage + " " + " ".join(["*"] * level)
        lines.append(f"    {ptr_type} {var}_p{level} = {prev};")
        prev = f"&{var}_p{level}"
    arg = prev  # &{var}_p{depth-1}: exactly `depth` stars
    if base == "void":
        arg = f"(void {' '.join(['*'] * param.depth)}){arg}"
    return (lines, arg, target)


def mock_controls(decl: str, symbol: str,
                  aggregates: set[str] | None = None) -> dict[str, Any] | None:
    """The control surface CMock will expose for *symbol*.

    ``{"return": bool, "out": [names], "out_aggregate": set, "out_depth": {name:
    depth}, "out_void": set}``. ``None`` when the declaration has a shape this
    generator refuses, so the caller records a limitation instead of emitting
    wrong code. ``out_depth``/``out_void`` let the caller refuse a mock
    write-through it cannot materialise faithfully (a scalar cannot supply the
    pointer value a ``T **`` out-parameter receives, and CMock has no working
    ``ReturnThruPtr`` for a ``void *`` pointee) — the same refusal contract the
    SUT parameter path follows.
    """
    split = split_declaration(decl)
    if split is None:
        return None
    ret, params = split
    pointers = [p for p in params if p.pointer]
    return {
        "return": ret not in _VOID,
        "out": [p.name for p in pointers],
        "out_aggregate": {p.name for p in pointers
                          if _base_type(p) in (aggregates or set())},
        "out_depth": {p.name: p.depth for p in pointers},
        "out_void": {p.name for p in pointers if _base_type(p) == "void"},
    }


def _is_c_expression(value: Any) -> bool:
    """True when *value* is the ``{"expr": "<C expression>"}`` escape hatch."""
    return isinstance(value, dict) and "expr" in value


def _case_problem(case, controls: dict[str, dict[str, Any]],
                  params: list[CParam], aggregates: set[str] | None = None,
                  declared_mocks: list[str] | None = None,
                  determined: bool = True) -> str | None:
    """Why *case* cannot be materialised faithfully, or ``None`` if it can.

    The plan is model output: it may ask for a control this generator does not
    implement. Refusing the scenario is the only honest option — dropping the
    control would make the mock return defaults and turn a planned expectation
    into a spurious test finding, i.e. a fabricated defect report.
    """
    # A parameter whose shape this generator cannot render faithfully (variadic,
    # function-pointer/array, or unparsed) must be refused *here*, not emitted as
    # an uncompilable call (opaque TOOL_FAILURE) or a scalar passed where the
    # callee dereferences it (a crash misread as an IMPLEMENTATION_DEFECT). Checked
    # before the arity test so the reason names the real cause, not a misleading
    # argument count (the scanner counts a trailing `...` as a parameter).
    shape_problem = _unsupported_param_shape(params)
    if shape_problem is not None:
        return shape_problem
    # The argument count must match the real signature: `signatures` come from the
    # deterministic scanner, not the model. A mismatch would emit an uncompilable
    # call and kill the whole bench as an opaque TOOL_FAILURE while still counting
    # the scenario as generated — the same refusal contract the collision and
    # unsupported-control cases already follow.
    if len(case.args) != len(params):
        return (f"the case supplies {len(case.args)} argument(s) but {len(params)} "
                "are declared; a case must give exactly one value per parameter")
    # A determined case that declares a dependency but configures no expectation
    # for it would leave CMock's strict mock unarmed: the first real call fails as
    # "called more times than expected", which would be misreported as an
    # IMPLEMENTATION_DEFECT when it is a harness gap. (`expect_calls: 0` is the
    # documented way to assert the dependency is NOT called.)
    if determined:
        for symbol in (declared_mocks or []):
            if symbol not in controls:
                continue
            if symbol not in (case.mocks or {}) and (case.expect_calls or {}).get(symbol) != 0:
                return (f"the case declares the mock {symbol!r} but sets no expectation "
                        f"for it; give `mocks`/`expect_calls` for {symbol!r} (use "
                        "`expect_calls: 0` to assert it is not called)")
    # An `expect_calls` entry naming a symbol the scenario does not mock would be
    # silently ignored (no expectation emitted), so a planned call-count is
    # dropped without a trace. Refuse it — the count would otherwise be unproven.
    for symbol in (case.expect_calls or {}):
        if symbol not in controls:
            return (f"`expect_calls` names {symbol!r}, which the scenario does not "
                    "mock; list it in `mocks` or remove the entry")
    for symbol, config in (case.mocks or {}).items():
        control = controls.get(symbol)
        if control is None:
            return f"mock {symbol!r} is not available"
        if not isinstance(config, dict):
            return f"mock {symbol!r} configuration is not an object"
        allowed = set(control["out"]) | {"ret"}
        for key, value in config.items():
            if key == "ret" and not control["return"]:
                return f"mock {symbol!r} has no return value but the case sets one"
            if key not in allowed:
                return (f"mock {symbol!r} sets unsupported control {key!r} "
                        f"(supported: {', '.join(sorted(allowed))})")
            # A mock out-parameter is written through by CMock's `ReturnThruPtr`,
            # which passes the *address* of a local of the pointee type. That is
            # faithful only for a single-indirection, non-void pointee: a `T **`
            # out-parameter would receive a `T *`, and a `void *` pointee has no
            # object to declare (`void var` is invalid C). Refuse this control with
            # a named limitation rather than emit wrong or uncompilable C — the
            # same contract the SUT parameter path follows. (The mock's declaration
            # alone is not refused: a case that configures no write-through still
            # works.)
            if key in control.get("out_void", set()):
                return (f"mock {symbol!r} writes through {key!r}, whose pointee is "
                        "`void`; a `void *` out-parameter cannot be supplied faithfully")
            if control.get("out_depth", {}).get(key, 1) >= 2:
                return (f"mock {symbol!r} writes through {key!r}, a multi-level "
                        "pointer; CMock's `ReturnThruPtr` cannot supply its value faithfully")
            if key in control["out_aggregate"] and not _is_c_expression(value):
                return (f"mock {symbol!r} sets {key!r}, whose pointee is an aggregate "
                        "type; supply it as {\"expr\": \"<C initializer>\"} instead of a scalar")
    pointer_indices = {i for i, p in enumerate(params) if p.pointer}
    aggregate_params = {i for i, p in enumerate(params)
                        if p.pointer and _base_type(p) in (aggregates or set())}
    for index in range(min(len(case.args), len(params))):
        if index in aggregate_params and not _is_c_expression(case.args[index]):
            return (f"argument {index} points at an aggregate type; supply its initial "
                    "value as {\"expr\": \"<C initializer>\"} instead of a scalar")
    for index, value in (case.expect_out or {}).items():
        try:
            position = int(index)
        except (TypeError, ValueError):
            return f"expect_out key {index!r} is not a parameter index"
        if position not in pointer_indices:
            return f"expect_out index {position} is not a pointer parameter of this function"
        # `expect_out` asserts the value the pointer points at after the call. For a
        # multi-level pointer (`int **pp`) the observable "value pointed at" is read
        # as the ultimately pointed-to scalar (`**pp`), which is the backing object
        # the emitter holds — the same reading as the depth-1 case. The parameter is
        # never refused for this: `void *` and multi-level pointers must stay
        # generatable (owner arbitration on the F1 finding).
        if position in aggregate_params and not _is_c_expression(value):
            return (f"expect_out index {position} points at an aggregate type; "
                    "supply it as {\"expr\": \"<C expression>\"} instead of a scalar")
    return None


# --- C value rendering ------------------------------------------------------


def _literal(value: Any) -> str:
    """Render a planned value as a C token.

    Numbers, booleans and **C identifiers** (enum constants such as
    ``SENSOR_ERROR``) pass through as C tokens; anything else becomes a C string
    literal. This is why the plan uses identifiers for enum-typed expectations.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if value is None:
        return "0"
    text = str(value)
    if text in ("true", "false") or re.fullmatch(r"-?\d+(\.\d+)?[uUlLfF]*", text):
        return text
    if re.fullmatch(r"[A-Za-z_]\w*", text):
        return text  # an enum constant or preprocessor symbol
    escaped = text.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _expression(value: Any) -> str:
    """Render a planned value that may use the ``{"expr": "..."}`` escape hatch."""
    return value["expr"] if _is_c_expression(value) else _literal(value)


# --- entry point ------------------------------------------------------------


def generate_suite(bench_root: Path, plan: Plan, inventory: Inventory,
                   *, excluded_units: set[str] | None = None) -> Generated:
    """Write a Ceedling project under ``<bench>/.mtm/gen`` from the plan.

    *excluded_units* are feasibility-BLOCKED unit ids: they are left out of the
    executable project (so they cannot fail the whole build) while remaining
    visible in the feasibility report and denominator (R03). A unit excluded for a
    *basename collision* (below) is added to the same exclusion set, so it too is
    driven out of the executable scope and recorded as a limitation.
    """
    excluded = set(excluded_units or set())
    blocked_ids = set(excluded)
    revision = Path(bench_root) / REVISION_DIRNAME
    if revision.exists():
        shutil.rmtree(revision)
    src_dir = revision / SOURCE_DIRNAME
    inc_dir = revision / "inc"
    shim_dir = revision / SHIM_DIRNAME
    iface_dir = revision / INTERFACE_DIRNAME
    test_dir = revision / TEST_DIRNAME
    for directory in (src_dir, inc_dir, shim_dir, iface_dir, test_dir):
        directory.mkdir(parents=True, exist_ok=True)

    out = Generated(revision=revision)
    units = {u.id: u for u in inventory.units}
    signatures = {fn["name"]: fn["signature"] for u in inventory.units for fn in u.functions}

    # 1. Shims first (types a dependency header may need come from here).
    shim_names: list[str] = []
    for name, content in sorted((plan.shim_headers or {}).items()):
        safe = Path(name).name
        if not safe.endswith(".h"):
            continue
        (shim_dir / safe).write_text(content, encoding="utf-8")
        out.files[f"{SHIM_DIRNAME}/{safe}"] = "shim"
        shim_names.append(safe)
    shim_includes = "".join(f'#include "{n}"\n' for n in shim_names)

    # 2. Copy the executable scope (selected sources + headers, minus blocked
    #    units) into the private source directory. The originals are never moved
    #    or edited; a copy keeps Ceedling's write output far from the inputs and
    #    makes each revision self-contained (no stale artefacts).
    #
    #    A flat revision directory cannot hold two files of the same basename, and
    #    a C `#include "x.h"` cannot be disambiguated between two directories that
    #    both provide `x.h`. Detect that collision and refuse the affected units
    #    with a recorded limitation, rather than silently overwriting one unit and
    #    reporting an inflated scenario count (the file-namespace equivalent of the
    #    generator's refusal contract).
    #
    #    Resolve directory scopes to their files first: a scope entry may name a
    #    directory, and its bytes (and any collision they carry) only become
    #    visible once expanded — otherwise a directory scope would silently copy
    #    nothing while still emitting a test file.
    scope_files = [p for p in resolve_scope_paths(bench_root, inventory.selected)
                   if p.suffix.lower() in (".c", ".h")]
    by_basename: dict[str, set[str]] = {}
    for candidate in scope_files:
        by_basename.setdefault(candidate.name, set()).add(str(candidate.resolve()))
    colliding = {name for name, paths in by_basename.items() if len(paths) > 1}
    colliding_units: list[tuple[str, str, str]] = []  # (unit_id, path, colliding name)
    for unit in inventory.units:
        if unit.id in excluded:
            continue
        # A unit is tainted by a collision if its own source basename collides, or
        # if a header it includes (local include) names a colliding file: the
        # compiled revision would silently bind it to an arbitrary one of them.
        own = Path(unit.path).name
        tainted = own if own in colliding else next(
            (inc for inc in unit.includes_local if Path(inc).name in colliding), None)
        if tainted:
            colliding_units.append((unit.id, unit.path, tainted))
            excluded = excluded | {unit.id}
    for unit_id, path, name in colliding_units:
        out.limitations.append(
            f"unit {unit_id} ({path}) is tainted by the basename collision on "
            f"{name!r}; a flat C revision cannot disambiguate the include, so it "
            "was excluded from the executable suite")

    copied: set[str] = set()
    for candidate in scope_files:
        if candidate.name in colliding:
            continue
        if candidate.suffix.lower() == ".c":
            owner = next((u for u in inventory.units if Path(u.path).name == candidate.name), None)
            if owner is not None and owner.id in excluded:
                continue
        if candidate.name in copied:
            continue
        copied.add(candidate.name)
        shutil.copy2(candidate, src_dir / candidate.name)
        out.files[f"{SOURCE_DIRNAME}/{candidate.name}"] = "source"

    # 3. One interface header per mocked symbol: CMock generates the mock from
    #    the header, so it declares only that symbol and the types it needs.
    interfaces: dict[str, str] = {}
    declarations: dict[str, str] = {}
    for scenario in plan.scenarios:
        if scenario.unit_id in excluded:
            continue
        for symbol in scenario.mocks:
            declared = scenario.mock_declarations.get(symbol) or ""
            if not declared:
                declared = _prototype_for_symbol(bench_root, inventory, symbol)
            declarations.setdefault(symbol, declared)
    for symbol, decl in sorted(declarations.items()):
        if not decl.strip():
            out.limitations.append(
                f"mocked symbol {symbol!r} has no declared interface; its "
                "scenarios are not executed")
            continue
        header_name = f"mtm_dep_{symbol}.h"
        interface_guard = f"MTM_DEP_{_ident(symbol).upper()}_H"
        body = (
            f"/* Private interface for the mocked dependency {symbol!r}.\n"
            f" * Declared from the plan/SDD so CMock can generate a mock. This\n"
            f" * header is NEVER written into the source directory. */\n"
            f"#ifndef {interface_guard}\n#define {interface_guard}\n"
            f"{shim_includes}#include <stdint.h>\n\n{decl.strip()}\n\n"
            f"#endif /* {interface_guard} */\n"
        )
        (iface_dir / header_name).write_text(body, encoding="utf-8")
        out.files[f"{INTERFACE_DIRNAME}/{header_name}"] = "interface"
        out.mocks.append(symbol)
        interfaces[symbol] = header_name

    # Aggregate aliases from every C text the project compiles against.
    aggregates: set[str] = set()
    for content in (plan.shim_headers or {}).values():
        aggregates |= aggregate_aliases(content)
    for decl in declarations.values():
        aggregates |= aggregate_aliases(decl)
    for name in sorted(copied):
        if name.endswith((".c", ".h")):
            aggregates |= aggregate_aliases((src_dir / name).read_text(encoding="utf-8", errors="replace"))

    # 4. One Unity test file per unit holding that unit's scenarios.
    by_unit: dict[str, list[Scenario]] = {}
    for scenario in plan.scenarios:
        by_unit.setdefault(scenario.unit_id, []).append(scenario)

    test_names: list[str] = []
    # Every excluded unit is recorded, whether or not the plan mentioned it, so a
    # blocked (or collision-excluded) source is never silently dropped (R03).
    for unit in inventory.units:
        if unit.id in blocked_ids:
            out.limitations.append(
                f"unit {unit.id} ({unit.path}) is BLOCKED and was excluded from "
                "the executable suite")
    for unit_id, scenarios in sorted(by_unit.items()):
        unit = units.get(unit_id)
        if unit is None or unit_id in excluded:
            continue
        # The test must include a header named after the source stem, else
        # Ceedling never links `<stem>.c` into the test executable (an opaque
        # `undefined reference`); a SUT declared only in a differently named header
        # still needs the stem header added. See `_unit_includes`.
        unit_includes = _unit_includes(unit, scenarios, copied, src_dir, iface_dir, out)
        if unit_includes is None:
            # No include set can both declare the SUT and link the source: the
            # limitation is already recorded, so skip the unit and let the rest of
            # the scope run rather than emit a mislinked test.
            continue
        code, names, skipped = _emit_test_file(
            unit, scenarios, signatures, interfaces, declarations, shim_names, aggregates,
            unit_includes=unit_includes)
        path = test_dir / f"test_{Path(unit.path).stem}.c"
        path.write_text(code, encoding="utf-8")
        out.files[f"{TEST_DIRNAME}/{path.name}"] = "test"
        test_names.extend(names)
        out.limitations.extend(skipped)

    # 5. The Ceedling project file.
    (revision / "project.yml").write_text(_project_yml(), encoding="utf-8")
    out.files["project.yml"] = "project"

    out.test_functions = test_names
    return out


def _ident(symbol: str) -> str:
    return re.sub(r"\W", "_", symbol)


def _synthesise_unit_header(unit, scenarios: list[Scenario], iface_dir: Path,
                            out: "Generated", *, header_name: str | None = None,
                            only: list[str] | None = None) -> str:
    """Materialise a private ``<stem>.h`` declaring the unit's functions under test.

    Ceedling (``test_<stem>.c``) links the source ``<stem>.c`` **only when the test
    includes ``<stem>.h``**, so the private header must carry the source's stem,
    not a synthetic name, or the translation unit never links. The declarations are
    taken from the deterministic scanner's own prototypes for this unit (never
    invented) and written under the private interface directory (on the include
    path, never into the source directory or the inputs). Returns the include name,
    or ``""`` when no prototype for a function under test could be produced.

    *header_name* overrides the stem name (used for the private *helper* header
    that declares only the functions a selected stem header omits); *only*
    restricts the declarations to that function set.
    """
    stem = Path(unit.path).stem
    header_name = header_name or f"{stem}.h"
    wanted = set(only) if only is not None else {sc.function for sc in scenarios}
    decls = [f"{fn['signature']};" for fn in getattr(unit, "functions", []) or []
             if fn.get("name") in wanted and fn.get("signature")]
    if not decls:
        out.limitations.append(
            f"unit {unit.id} ({unit.path}) cannot be materialised: no scanned "
            "prototype exists for its functions under test")
        return ""
    guard = f"MTM_UNIT_{_ident(header_name.rsplit('.', 1)[0]).upper()}_H"
    body = (
        f"/* Private declarations for {unit.path} — its functions under test are\n"
        " * declared so the test can call them and Ceedling links the translation\n"
        " * unit (a `<stem>.h` include is required for that). Prototypes are taken\n"
        " * verbatim from the input scanner (never invented). This header is NEVER\n"
        " * written into the source directory. */\n"
        f"#ifndef {guard}\n#define {guard}\n#include <stdint.h>\n\n"
        + "\n".join(decls) + f"\n\n#endif /* {guard} */\n"
    )
    (iface_dir / header_name).write_text(body, encoding="utf-8")
    out.files[f"{INTERFACE_DIRNAME}/{header_name}"] = "interface"
    return header_name


def test_name_for(unit_id: str, scenario_id: str) -> str:
    """The Unity test function name for a scenario (one place, one rule).

    Both the emitter and the finding→scenario mapper use this, so a scenario id
    containing ``-`` or ``_`` is never mis-split when a finding is attributed back
    to its oracle (the id is not reversed; the name is looked up in the plan).
    """
    return f"test_{unit_id.replace('unit-', 'u')}_{scenario_id}".replace("-", "_")


def _prototype_for_symbol(bench_root: Path, inventory: Inventory, symbol: str) -> str:
    """Deterministically derive a dependency's prototype from the selected headers.

    Directory scopes are expanded first, so a header selected through a directory
    contributes its prototypes just as a directly-selected header does.
    """
    from .cscan import scan_prototypes

    for candidate in resolve_scope_paths(bench_root, inventory.selected):
        if candidate.suffix.lower() == ".h":
            for name, decl in scan_prototypes(candidate):
                if name == symbol:
                    return decl
    return ""


def _project_yml() -> str:
    """The Ceedling project file for a generated suite.

    ``:enforce_strict_ordering: FALSE`` keeps a scenario's expectations in the
    order written without demanding global ordering across mocks; the mock
    plugins provide the expressiveness the plan uses (return values, out
    parameters, unlimited coverage calls for UNDETERMINED cases).
    """
    return (
        ":project:\n"
        "  :use_exceptions: FALSE\n"
        "  :use_mocks: TRUE\n"
        "  :use_test_preprocessor: :none\n"
        "  :build_root: build\n"
        "  :test_file_prefix: test_\n"
        "  :which_ceedling: gem\n"
        ":paths:\n"
        "  :source:\n"
        f"    - {SOURCE_DIRNAME}/**\n"
        "  :include:\n"
        f"    - {SHIM_DIRNAME}/**\n"
        f"    - {INTERFACE_DIRNAME}/**\n"
        f"    - {SOURCE_DIRNAME}/**\n"
        "  :test:\n"
        f"    - {TEST_DIRNAME}/**\n"
        ":cmock:\n"
        "  :mock_prefix: mock_\n"
        "  :when_no_prototypes: :warn\n"
        "  :enforce_strict_ordering: FALSE\n"
        "  :plugins:\n"
        "    - :ignore\n"
        "    - :expect_any_args\n"
        "    - :return_thru_ptr\n"
        "    - :ignore_arg\n"
        "    - :callback\n"
    )


# --- test emission ----------------------------------------------------------


def _mock_header_name(symbol: str, interfaces: dict[str, str]) -> str:
    """The CMock-generated header a test includes for *symbol*."""
    return f"mock_{interfaces[symbol]}"


def _emit_mock_setup(symbol: str, config: dict[str, Any], control: dict[str, Any],
                     case_index: int, determined: bool,
                     lines: list[str]) -> dict[str, str]:
    """Emit CMock expectation/ignore calls for one mock, one case.

    Returns ``{param_name: variable}`` for the out-parameter values it declared.
    A determined (SPECIFIED/ASSUMED) case sets an **exactly-N-call expectation**;
    an UNDETERMINED case uses an *ignore* (unlimited calls, no assertion), so a
    coverage-only scenario never produces a conformity verdict (B4).
    """
    config = config if isinstance(config, dict) else {}
    out_vars: dict[str, str] = {}
    # Declare a local for each out-parameter value the plan supplies, so the
    # mock can copy it through the pointer on the call.
    for name in control["out"]:
        if name in config:
            var = f"mock_{case_index}_{_ident(symbol)}_{name}"
            ptype = control["out_types"].get(name, "int")
            lines.append(f"    {ptype} {var} = {_expression(config[name])};")
            out_vars[name] = var

    count = 1
    if determined:
        raw = config.get("_calls")
        # Preserve an explicit zero: `expect_calls: 0` must emit NO expectation so
        # CMock's strictness makes any real call fail. A truthy-only guard would
        # silently turn 0 into one expected call and accept a forbidden call.
        count = raw if isinstance(raw, int) and raw >= 0 else 1

    def emit_one_expectation() -> None:
        if determined:
            if control["return"]:
                lines.append(f"    {symbol}_ExpectAnyArgsAndReturn({_expression(config.get('ret', 0))});")
            else:
                lines.append(f"    {symbol}_ExpectAnyArgs();")
        else:
            if control["return"]:
                lines.append(f"    {symbol}_IgnoreAndReturn({_expression(config.get('ret', 0))});")
            else:
                lines.append(f"    {symbol}_Ignore();")
        for name, var in out_vars.items():
            lines.append(f"    {symbol}_ReturnThruPtr_{name}(&{var});")

    if determined and count == 0:
        # No expectation: CMock's strictness makes any unexpected call fail.
        return out_vars
    if determined:
        for _ in range(count):
            emit_one_expectation()
    else:
        emit_one_expectation()
    return out_vars


def _emit_test_file(unit, scenarios: list[Scenario], signatures: dict[str, str],
                    interfaces: dict[str, str], declarations: dict[str, str],
                    shim_names: list[str] | None = None,
                    aggregates: set[str] | None = None,
                    unit_includes: list[str] | None = None) -> tuple[str, list[str], list[str]]:
    unit_includes = list(unit_includes or [])
    lines: list[str] = [
        f"/* Generated Unity tests for {unit.path} — materialised from the plan. */",
        '#include "unity.h"',
        "#include <stdint.h>",
        "#include <stdbool.h>",
        "#include <stddef.h>",
    ]
    used_mocks = sorted({s for sc in scenarios for s in sc.mocks if s in interfaces})
    # Private shim headers first, then the CMock mock headers, then the unit's
    # declaration header(s) — the stem header must be present so Ceedling links
    # `<stem>.c` into the test executable.
    lines += [f'#include "{name}"' for name in (shim_names or [])]
    lines += [f'#include "{_mock_header_name(s, interfaces)}"' for s in used_mocks]
    lines += [f'#include "{name}"' for name in unit_includes]
    lines += [""]

    names: list[str] = []
    skipped: list[str] = []
    emitted: set[str] = set()
    for scenario in scenarios:
        fn = scenario.function
        signature = signatures.get(fn, "")
        params = func_params(signature)
        void_ret = return_is_void(signature)
        test_name = test_name_for(scenario.unit_id, scenario.id)
        controls: dict[str, dict[str, Any]] = {}
        problem: str | None = None
        # A scenario with no cases would emit an empty test function that Unity
        # counts as a pass ("1 Tests 0 Failures") without exercising any C — a
        # vacuous SUCCEEDED. Refuse it as a limitation instead.
        if not scenario.cases:
            skipped.append(
                f"scenario {scenario.id} of {fn} not generated: it declares no cases, "
                "so it would exercise no C behaviour")
            continue
        for symbol in scenario.mocks:
            control = mock_controls(declarations.get(symbol, ""), symbol, aggregates)
            if control is None:
                problem = f"mock {symbol!r} has no usable declaration"
                break
            split = split_declaration(declarations.get(symbol, ""))
            control["out_types"] = {p.name: _base_type(p) for p in split[1]} if split else {}
            controls[symbol] = control
        if problem is None:
            declared_mocks = [s for s in scenario.mocks if s in controls]
            scenario_determined = not scenario.oracle.is_undetermined
            for case in scenario.cases:
                problem = _case_problem(case, {s: {k: v for k, v in c.items() if k != "out_types"}
                                               for s, c in controls.items()}, params, aggregates,
                                        declared_mocks=declared_mocks,
                                        determined=scenario_determined)
                if problem:
                    break
        if problem is not None:
            skipped.append(f"scenario {scenario.id} of {fn} not generated: {problem}")
            continue
        # Two scenario ids distinct in the plan can normalise (`-`/`:`/`_` -> `_`)
        # to one C symbol; emitting both would be a duplicate definition, so the
        # later one is refused with a recorded limitation rather than failing the
        # whole unit as an opaque compile error.
        if test_name in emitted:
            skipped.append(
                f"scenario {scenario.id} of {fn} not generated: its test name "
                f"{test_name!r} collides with another scenario's after "
                "normalisation; rename the scenario id")
            continue
        emitted.add(test_name)

        determined = not scenario.oracle.is_undetermined
        names.append(test_name)
        lines.append(f"/* {scenario.id}: {scenario.title} — oracle {scenario.oracle.kind} */")
        lines.append(f"void {test_name}(void)")
        lines.append("{")
        for case_index, case in enumerate(scenario.cases):
            # Declare the unit's own pointer arguments (one per case: they share a
            # function scope). A planned `args` entry for a pointer parameter is
            # its INITIAL pointed-to value, so a case can assert that a path
            # preserves the previously accepted value. The argument is passed at
            # the **declared indirection depth** (`int **` gets an `int **`, not an
            # `int *`), built from correctly-typed backing storage — never a scalar
            # where a pointer is declared.
            args: list[str] = []
            outs: dict[int, str] = {}
            for index, value in enumerate(case.args):
                param = params[index] if index < len(params) else CParam("", "", 0)
                if param.pointer:
                    var = f"out{case_index}_{index}"
                    decl_lines, arg, target = _pointer_arg(param, var, value)
                    lines.extend(decl_lines)
                    args.append(arg)
                    # `expect_out` asserts the ultimate pointed-to scalar, which is
                    # the `_v0` local at every depth.
                    outs[index] = target
                else:
                    args.append(_cast(param, value))
            # Mock expectations are set before the call they govern.
            for symbol, config in (case.mocks or {}).items():
                if symbol not in controls:
                    continue
                cfg = dict(config) if isinstance(config, dict) else {}
                cfg["_calls"] = (case.expect_calls or {}).get(symbol)
                _emit_mock_setup(symbol, cfg, controls[symbol], case_index, determined, lines)
            call = f"{fn}({', '.join(args)})"
            if case.expected is None:
                lines.append("    /* UNDETERMINED: executed for coverage only, no conformity claim */")
                lines.append(f"    (void){call};" if not void_ret else f"    {call};")
            elif void_ret:
                lines.append(f"    {call};")
            else:
                lines.append(f"    TEST_ASSERT_EQUAL({_literal(case.expected)}, {call});")
            for index_str, expected in (case.expect_out or {}).items():
                var = outs.get(int(index_str))
                if var is not None:
                    lines.append(f"    TEST_ASSERT_EQUAL({_literal(expected)}, {var});")
        lines.append("}")
        lines.append("")

    lines += ["void setUp(void) {}", "void tearDown(void) {}"]
    return "\n".join(lines) + "\n", names, skipped
