"""Deterministic test-suite generation from an accepted plan.

Given a grounded :class:`~mtm.plan.Plan`, this module emits **compilable** Unity
test files, private mocks for link-time externals and a runner — purely by
rendering the plan. The model never writes C here, so it cannot weaken an oracle
between the reviewed plan and the executed assertion (``automation-policy.md``:
the plan is generated *before* the suite and the suite is a faithful
materialisation of it).

Everything is emitted under the bench, never into the selected inputs: the
original C/SDD bytes are untouched (SYS-004). A symbol the generator cannot mock
faithfully is reported as a limitation, never silently stubbed into a passing
test.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .cscan import scan_prototypes
from .inventory import Inventory
from .plan import Plan, Scenario

_VOID = {"void", ""}
_CTYPE_WORDS = (
    "unsigned", "signed", "long", "short", "int", "char", "float", "double",
    "bool", "_Bool",
)


@dataclass
class CParam:
    type: str
    name: str
    pointer: bool


@dataclass
class Generated:
    """The materialised suite manifest."""

    files: dict[str, str] = field(default_factory=dict)  # relpath -> role
    test_functions: list[str] = field(default_factory=list)
    mocks: list[str] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
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


# --- entry point ------------------------------------------------------------


def generate_suite(bench_root: Path, plan: Plan, inventory: Inventory) -> Generated:
    """Write the generated suite under ``<bench>/tests`` and ``<bench>/mocks``."""
    tests_dir = bench_root / "tests"
    mocks_dir = bench_root / "mocks"
    tests_dir.mkdir(parents=True, exist_ok=True)
    mocks_dir.mkdir(parents=True, exist_ok=True)

    units = {u.id: u for u in inventory.units}
    signatures = {fn["name"]: fn["signature"] for u in inventory.units for fn in u.functions}

    # Deterministically derive declarations for mocked symbols from the selected
    # headers, so a mock never depends on the model restating an interface.
    prototypes: dict[str, str] = {}
    for entry in inventory.selected:
        candidate = bench_root / str(entry.get("path", ""))
        if candidate.suffix.lower() == ".h" and candidate.is_file():
            for name, decl in scan_prototypes(candidate):
                prototypes.setdefault(name, decl)

    out = Generated()

    # 0. Private shim headers for absent includes (proposed by the plan). Written
    #    under the bench only; the source directory and the missing header are
    #    never created or modified.
    shims_dir = bench_root / "shims"
    shim_names = sorted(Path(n).name for n in (plan.shim_headers or {}) if str(n).endswith(".h"))
    for name, content in sorted((plan.shim_headers or {}).items()):
        safe = Path(name).name
        if not safe.endswith(".h"):
            continue
        shims_dir.mkdir(parents=True, exist_ok=True)
        (shims_dir / safe).write_text(content, encoding="utf-8")
        out.files[f"shims/{safe}"] = "shim"

    # 1. Mocks: at most one file per external symbol, shared by scenarios.
    declarations: dict[str, str] = {}
    for scenario in plan.scenarios:
        for symbol in scenario.mocks:
            declared = scenario.mock_declarations.get(symbol) or prototypes.get(symbol, "")
            declarations.setdefault(symbol, declared)
    mock_headers: dict[str, str] = {}
    for symbol, decl in sorted(declarations.items()):
        rendered, header = _emit_mock(symbol, decl, shim_names)
        if rendered is None:
            out.limitations.append(
                f"mocked symbol {symbol!r} has an interface this generator does not "
                "support (or none was declared); its scenarios are not executed"
            )
            continue
        (mocks_dir / f"mock_{symbol}.c").write_text(rendered, encoding="utf-8")
        out.files[f"mocks/mock_{symbol}.c"] = "mock"
        out.mocks.append(symbol)
        mock_headers[symbol] = header

    # 2. One test file per unit holding that unit's scenarios. Aggregate type
    #    aliases are collected from every C text this suite compiles against, so a
    #    pointer-to-aggregate parameter is never initialised from a bare scalar.
    aggregates: set[str] = set()
    for content in (plan.shim_headers or {}).values():
        aggregates |= aggregate_aliases(content)
    for entry in inventory.selected:
        candidate = bench_root / str(entry.get("path", ""))
        if candidate.suffix.lower() in (".h", ".c") and candidate.is_file():
            aggregates |= aggregate_aliases(candidate.read_text(encoding="utf-8", errors="replace"))
    for decl in declarations.values():
        aggregates |= aggregate_aliases(decl)

    by_unit: dict[str, list[Scenario]] = {}
    for scenario in plan.scenarios:
        by_unit.setdefault(scenario.unit_id, []).append(scenario)

    test_names: list[str] = []
    for unit_id, scenarios in sorted(by_unit.items()):
        unit = units.get(unit_id)
        if unit is None:
            continue
        code, names, skipped = _emit_test_file(
            unit, scenarios, signatures, mock_headers, declarations, shim_names, aggregates)
        path = tests_dir / f"{Path(unit.path).stem}_mtm_test.c"
        path.write_text(code, encoding="utf-8")
        out.files[f"tests/{path.name}"] = "test"
        test_names.extend(names)
        out.limitations.extend(skipped)

    out.test_functions = test_names
    runner_path = tests_dir / "mtm_runner.c"
    runner_path.write_text(_emit_runner(test_names), encoding="utf-8")
    out.files["tests/mtm_runner.c"] = "runner"

    manifest = {
        "files": out.files,
        "test_functions": out.test_functions,
        "mocks": out.mocks,
        "limitations": out.limitations,
    }
    import json

    (tests_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True), encoding="utf-8")
    out.files["tests/manifest.json"] = "manifest"
    return out


# --- mocks ------------------------------------------------------------------


def _strip_c_comments(text: str) -> str:
    """Remove C comments from model-supplied C text (an untrusted declaration).

    A ``mock_declarations`` entry legitimately explains its provenance in a
    comment, but a comment inside a declaration would break parsing and, worse,
    would be emitted verbatim into generated C. Strip it here so only the
    declaration itself survives.
    """
    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.DOTALL)
    return re.sub(r"//[^\n]*", " ", text)


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


def mock_controls(decl: str, symbol: str,
                  aggregates: set[str] | None = None) -> dict[str, Any] | None:
    """The control surface :func:`_emit_mock` will expose for *symbol*.

    ``{"return": bool, "out": [pointer names], "out_aggregate": {pointer names}}``.
    ``None`` when the declaration has a shape this generator refuses, so the
    caller records a limitation instead of emitting wrong code.
    """
    decl = _strip_c_comments(decl or "").strip().rstrip(";")
    if not decl:
        return None
    m = re.match(r"^(?P<ret>.+?)\b(?P<name>[A-Za-z_]\w*)\s*\((?P<params>.*)\)\s*$", decl)
    if not m or m.group("name") != symbol:
        return None
    ret = m.group("ret").strip()
    if "*" in ret or "..." in m.group("params"):
        return None
    params = split_params(m.group("params"))
    for p in params:
        if not p.name or p.type.endswith("*") or "(" in p.type:
            return None
    return {"return": ret not in _VOID, "out": [p.name for p in params if p.pointer],
            "out_aggregate": {p.name for p in params
                              if p.pointer and p.type.strip().rstrip("*").strip() in (aggregates or set())}}


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
                        if p.pointer and p.type.strip().rstrip("*").strip() in (aggregates or set())}
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


def _emit_mock(symbol: str, decl: str, shim_names: list[str] | None = None) -> tuple[str | None, str]:
    """Emit a controllable mock for *symbol*.

    Supported shapes: scalar (incl. enum/``_t``) return, scalar or pointer
    parameters. Aggregates-by-value and variadics are refused (``None``) so a
    caller records a limitation instead of generating wrong code.
    """
    decl = _strip_c_comments(decl or "").strip().rstrip(";")
    if not decl:
        return None, ""
    m = re.match(r"^(?P<ret>.+?)\b(?P<name>[A-Za-z_]\w*)\s*\((?P<params>.*)\)\s*$", decl)
    if not m or m.group("name") != symbol:
        return None, ""
    ret = m.group("ret").strip()
    if "*" in ret or "..." in m.group("params"):
        return None, ""
    params = split_params(m.group("params"))
    for p in params:
        if not p.name or p.type.endswith("*") or "(" in p.type:
            return None, ""

    includes = _libc_includes_for([ret] + [p.type for p in params])
    includes = [f'#include "{name}"' for name in (shim_names or [])] + includes
    header_lines = [f"void mock_{symbol}_reset(void);", f"int mock_{symbol}_calls(void);"]
    if ret not in _VOID:
        header_lines.append(f"void mock_{symbol}_set_ret({ret} v);")
    for p in params:
        if p.pointer:
            header_lines.append(f"void mock_{symbol}_set_{p.name}({p.type} v);")
    header = "\n".join(header_lines)

    lines: list[str] = [
        f"/* Generated controllable mock for {symbol!r} — do not edit by hand. */",
        *includes,
        "",
        f"extern {decl};",
        "",
        "static int mock_calls = 0;",
    ]
    if ret not in _VOID:
        lines.append(f"static {ret} mock_ret;")
    for p in params:
        if p.pointer:
            # Zero-initialised: `static` storage already zeroes scalars and
            # aggregates alike, so a struct-typed pointee works too.
            lines.append(f"static {p.type} mock_out_{p.name};")
    lines += ["", f"void mock_{symbol}_reset(void) {{", "    mock_calls = 0;"]
    if ret not in _VOID:
        lines.append(f"    mock_ret = ({ret}){{0}};")
    for p in params:
        if p.pointer:
            lines.append(f"    mock_out_{p.name} = ({p.type}){{0}};")
    lines += ["}", ""]
    if ret not in _VOID:
        lines.append(f"void mock_{symbol}_set_ret({ret} v) {{ mock_ret = v; }}")
    for p in params:
        if p.pointer:
            lines.append(f"void mock_{symbol}_set_{p.name}({p.type} v) {{ mock_out_{p.name} = v; }}")
    lines += [f"int mock_{symbol}_calls(void) {{ return mock_calls; }}", ""]
    call_args = ", ".join(f"{p.type} *{p.name}" if p.pointer else f"{p.type} {p.name}"
                          for p in params) or "void"
    lines.append(f"{ret} {symbol}({call_args}) {{")
    lines.append("    mock_calls += 1;")
    for p in params:
        if p.pointer:
            lines.append(f"    if ({p.name}) *{p.name} = mock_out_{p.name};")
        else:
            lines.append(f"    (void){p.name};")
    if ret not in _VOID:
        lines.append("    return mock_ret;")
    lines.append("}")
    return "\n".join(lines) + "\n", header


def _libc_includes_for(types: list[str]) -> list[str]:
    joined = " ".join(types)
    need: set[str] = set()
    if "uint" in joined or re.search(r"\bint\b", joined):
        need.add("#include <stdint.h>")
    if "size_t" in joined:
        need.add("#include <stddef.h>")
    if "bool" in joined:
        need.add("#include <stdbool.h>")
    return sorted(need)


# --- tests ------------------------------------------------------------------


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


def _cast(param: CParam, value: Any) -> str:
    return f"({param.type}){_literal(value)}"


def _emit_test_file(unit, scenarios: list[Scenario], signatures: dict[str, str],
                    mock_headers: dict[str, str], mock_decls: dict[str, str],
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
    # Private shim headers first, so types a mock control function uses are known.
    lines += [f'#include "{name}"' for name in (shim_names or [])]
    lines += [
        f'#include "{unit_header}"',
        "",
    ]
    used_mocks = sorted({s for sc in scenarios for s in sc.mocks if s in mock_headers})
    for symbol in used_mocks:
        lines.append(f"/* mock control: {symbol} */")
        lines.append(mock_headers[symbol])
    lines.append("")

    names: list[str] = []
    skipped: list[str] = []
    for scenario in scenarios:
        fn = scenario.function
        signature = signatures.get(fn, "")
        params = func_params(signature)
        void_ret = return_is_void(signature)
        test_name = f"test_{scenario.unit_id.replace('unit-', 'u')}_{scenario.id}".replace("-", "_")
        controls = {
            symbol: mock_controls(mock_decls.get(symbol, ""), symbol, aggregates)
            for symbol in scenario.mocks
        }
        # A scenario the generator cannot materialise faithfully is reported as a
        # limitation, never emitted as a test: it must not become a silent
        # default-driven assertion that invents a defect.
        problem = None
        for symbol, control in controls.items():
            if control is None:
                problem = f"mock {symbol!r} has no usable declaration"
                break
        if problem is None:
            for case in scenario.cases:
                problem = _case_problem(case, controls, params, aggregates)
                if problem:
                    break
        if problem is not None:
            skipped.append(f"scenario {scenario.id} of {fn} not generated: {problem}")
            continue
        names.append(test_name)
        lines.append(f"/* {scenario.id}: {scenario.title} — oracle {scenario.oracle.kind} */")
        lines.append(f"void {test_name}(void)")
        lines.append("{")
        scenario_mocks = [s for s in used_mocks if s in (scenario.mocks or [])]
        for case_index, case in enumerate(scenario.cases):
            # Reset every mock the scenario uses before each case: `expect_calls`
            # is per case, and a case must not inherit the previous one's count.
            for symbol in scenario_mocks:
                lines.append(f"    mock_{symbol}_reset();")
            for symbol, config in (case.mocks or {}).items():
                config = config if isinstance(config, dict) else {}
                if "ret" in config:
                    lines.append(f"    mock_{symbol}_set_ret({_literal(config['ret'])});")
                for key, value in config.items():
                    if key == "ret":
                        continue
                    # A value of the form {"expr": "..."} is emitted verbatim as a
                    # C expression (needed for aggregate pointees, e.g. a struct).
                    expr = value["expr"] if isinstance(value, dict) and "expr" in value else _literal(value)
                    lines.append(f"    mock_{symbol}_set_{key}({expr});")
            args: list[str] = []
            outs: dict[int, str] = {}
            for index, value in enumerate(case.args):
                param = params[index] if index < len(params) else CParam("", "", False)
                if param.pointer:
                    # One variable per case: several cases share a function scope.
                    # A planned `args` entry for a pointer parameter is its INITIAL
                    # pointed-to value, so a case can assert that a path preserves
                    # the previously accepted reading.
                    var = f"out{case_index}_{index}"
                    lines.append(f"    {param.type} {var} = {_cast(param, value)};")
                    args.append(f"&{var}")
                    outs[index] = var
                else:
                    args.append(_cast(param, value))
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
            for symbol, count in (case.expect_calls or {}).items():
                lines.append(f"    TEST_ASSERT_EQUAL_INT({_literal(count)}, mock_{symbol}_calls());")
        lines.append("}")
        lines.append("")

    lines += ["void setUp(void) {}", "void tearDown(void) {}"]
    return "\n".join(lines) + "\n", names, skipped


def _emit_runner(test_names: list[str]) -> str:
    lines = ["/* Generated Unity runner. */", '#include "unity.h"', ""]
    lines += [f"void {name}(void);" for name in test_names]
    lines += ["", "int main(void)", "{", "    UNITY_BEGIN();"]
    lines += [f"    RUN_TEST({name});" for name in test_names]
    lines += ["    return UNITY_END();", "}"]
    return "\n".join(lines) + "\n"
