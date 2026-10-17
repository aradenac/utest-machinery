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
from .inventory import Inventory
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
    type: str
    name: str
    pointer: bool


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
    text = text.strip()
    pointer = "*" in text
    core = text.replace("*", " ").strip()
    tokens = core.split()
    if not tokens:
        return CParam(type="", name="", pointer=pointer)
    if len(tokens) >= 2 and tokens[-1] not in _CTYPE_WORDS and not _looks_like_type(tokens[-1]):
        name, type_ = tokens[-1], " ".join(tokens[:-1])
    elif len(tokens) == 1:
        name, type_ = "", tokens[0]
    else:
        name, type_ = "", core
    return CParam(type=type_.strip(), name=name, pointer=pointer)


def _looks_like_type(token: str) -> bool:
    return token.endswith("_t") or token in _CTYPE_WORDS


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


def _unit_header(unit) -> str:
    """The header a test file should include for *unit*.

    Prefers a selected header with the same stem (``foo.c`` -> ``foo.h``) so the
    test links against the compiled translation unit; otherwise falls back to the
    source path so a headerless fixture still builds.
    """
    for inc in getattr(unit, "includes_local", []) or []:
        if inc.endswith(".h"):
            return inc
    stem = Path(unit.path).stem
    return f"{stem}.h" if stem else Path(unit.path).name


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


def mock_controls(decl: str, symbol: str,
                  aggregates: set[str] | None = None) -> dict[str, Any] | None:
    """The control surface CMock will expose for *symbol*.

    ``{"return": bool, "out": [pointer names], "out_aggregate": {pointer names}}``.
    ``None`` when the declaration has a shape this generator refuses, so the
    caller records a limitation instead of emitting wrong code.
    """
    split = split_declaration(decl)
    if split is None:
        return None
    ret, params = split
    return {
        "return": ret not in _VOID,
        "out": [p.name for p in params if p.pointer],
        "out_aggregate": {p.name for p in params
                          if p.pointer and _base_type(p) in (aggregates or set())},
    }


def _is_c_expression(value: Any) -> bool:
    """True when *value* is the ``{"expr": "<C expression>"}`` escape hatch."""
    return isinstance(value, dict) and "expr" in value


def _case_problem(case, controls: dict[str, dict[str, Any]],
                  params: list[CParam], aggregates: set[str] | None = None) -> str | None:
    """Why *case* cannot be materialised faithfully, or ``None`` if it can.

    The plan is model output: it may ask for a control this generator does not
    implement. Refusing the scenario is the only honest option — dropping the
    control would make the mock return defaults and turn a planned expectation
    into a spurious test finding, i.e. a fabricated defect report.
    """
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


def _cast(param: CParam, value: Any) -> str:
    return f"({param.type}){_expression(value)}"


# --- entry point ------------------------------------------------------------


def generate_suite(bench_root: Path, plan: Plan, inventory: Inventory,
                   *, excluded_units: set[str] | None = None) -> Generated:
    """Write a Ceedling project under ``<bench>/.mtm/gen`` from the plan.

    *excluded_units* are feasibility-BLOCKED unit ids: they are left out of the
    executable project (so they cannot fail the whole build) while remaining
    visible in the feasibility report and denominator (R03).
    """
    excluded = excluded_units or set()
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
    copied: set[str] = set()
    blocked_headers: set[str] = set()
    for unit in inventory.units:
        if unit.id in excluded:
            blocked_headers.add(Path(unit.path).stem)
    for entry in inventory.selected:
        candidate = bench_root / str(entry.get("path", ""))
        if not candidate.is_file() or candidate.suffix.lower() not in (".c", ".h"):
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
    # blocked source is never silently dropped from the run (R03).
    for unit in inventory.units:
        if unit.id in excluded:
            out.limitations.append(
                f"unit {unit.id} ({unit.path}) is BLOCKED and was excluded from "
                "the executable suite")
    for unit_id, scenarios in sorted(by_unit.items()):
        unit = units.get(unit_id)
        if unit is None or unit_id in excluded:
            continue
        code, names, skipped = _emit_test_file(
            unit, scenarios, signatures, interfaces, declarations, shim_names, aggregates)
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


def _prototype_for_symbol(bench_root: Path, inventory: Inventory, symbol: str) -> str:
    """Deterministically derive a dependency's prototype from the selected headers."""
    from .cscan import scan_prototypes

    for entry in inventory.selected:
        candidate = bench_root / str(entry.get("path", ""))
        if candidate.suffix.lower() == ".h" and candidate.is_file():
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
        count = int(raw) if isinstance(raw, int) and raw > 0 else 1

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
                    aggregates: set[str] | None = None) -> tuple[str, list[str], list[str]]:
    unit_header = _unit_header(unit)
    lines: list[str] = [
        f"/* Generated Unity tests for {unit.path} — materialised from the plan. */",
        '#include "unity.h"',
        "#include <stdint.h>",
        "#include <stdbool.h>",
        "#include <stddef.h>",
    ]
    used_mocks = sorted({s for sc in scenarios for s in sc.mocks if s in interfaces})
    # Private shim headers first, then the CMock mock headers, then the unit.
    lines += [f'#include "{name}"' for name in (shim_names or [])]
    lines += [f'#include "{_mock_header_name(s, interfaces)}"' for s in used_mocks]
    lines += [f'#include "{unit_header}"', ""]

    names: list[str] = []
    skipped: list[str] = []
    for scenario in scenarios:
        fn = scenario.function
        signature = signatures.get(fn, "")
        params = func_params(signature)
        void_ret = return_is_void(signature)
        test_name = f"test_{scenario.unit_id.replace('unit-', 'u')}_{scenario.id}".replace("-", "_")
        controls: dict[str, dict[str, Any]] = {}
        problem: str | None = None
        for symbol in scenario.mocks:
            control = mock_controls(declarations.get(symbol, ""), symbol, aggregates)
            if control is None:
                problem = f"mock {symbol!r} has no usable declaration"
                break
            split = split_declaration(declarations.get(symbol, ""))
            control["out_types"] = {p.name: _base_type(p) for p in split[1]} if split else {}
            controls[symbol] = control
        if problem is None:
            for case in scenario.cases:
                problem = _case_problem(case, {s: {k: v for k, v in c.items() if k != "out_types"}
                                               for s, c in controls.items()}, params, aggregates)
                if problem:
                    break
        if problem is not None:
            skipped.append(f"scenario {scenario.id} of {fn} not generated: {problem}")
            continue

        determined = not scenario.oracle.is_undetermined
        names.append(test_name)
        lines.append(f"/* {scenario.id}: {scenario.title} — oracle {scenario.oracle.kind} */")
        lines.append(f"void {test_name}(void)")
        lines.append("{")
        for case_index, case in enumerate(scenario.cases):
            # Declare the unit's own pointer arguments (one per case: they share a
            # function scope). A planned `args` entry for a pointer parameter is
            # its INITIAL pointed-to value, so a case can assert that a path
            # preserves the previously accepted value.
            args: list[str] = []
            outs: dict[int, str] = {}
            for index, value in enumerate(case.args):
                param = params[index] if index < len(params) else CParam("", "", False)
                if param.pointer:
                    var = f"out{case_index}_{index}"
                    lines.append(f"    {param.type} {var} = {_cast(param, value)};")
                    args.append(f"&{var}")
                    outs[index] = var
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
