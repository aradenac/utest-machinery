"""PR 2 unit tests: scanner, inventory, oracle grounding and the LLM provider.

These are fast, offline and deterministic — no Docker, no token.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from mtm.cscan import external_calls, scan_c, scan_prototypes, strip_comments_and_strings
from mtm.inventory import build_inventory, extract_requirements, snapshot_hash
from mtm.llm import MockProvider, make_provider
from mtm.plan import ASSUMED, SPECIFIED, UNDETERMINED, build_prompt, parse_plan
from mtm.errors import MtmError

REPO = Path(__file__).resolve().parents[1]
EXAMPLES = REPO / "examples"


def _selected(fixture: str) -> list[dict]:
    directory = EXAMPLES / fixture
    return [
        {"path": str(p), "kind": "source" if p.suffix == ".c" else "spec"}
        for p in sorted(directory.iterdir()) if p.is_file()
    ]


# --- scanner ----------------------------------------------------------------


def test_scan_finds_function_definitions_across_line_breaks():
    unit = scan_c(EXAMPLES / "frame-parser" / "frame.c")
    assert [f.name for f in unit.functions] == ["parse_frame"]
    assert unit.functions[0].return_type == "int"


def test_scan_finds_definition_with_return_on_previous_line():
    unit = scan_c(EXAMPLES / "saturation" / "saturation.c")
    assert [f.name for f in unit.functions] == ["saturate_u16"]


def test_strip_comments_keeps_line_numbers():
    text = "a\n/* x\ny */b\n// c\nd\n"
    stripped = strip_comments_and_strings(text)
    assert stripped.count("\n") == text.count("\n")


def test_external_calls_exclude_libc_and_definitions():
    unit = scan_c(EXAMPLES / "mocked-sensor" / "sensor.c")
    assert external_calls(unit) == ["platform_read_sensor"]


def test_scan_prototypes_reads_header_declarations():
    protos = dict(scan_prototypes(EXAMPLES / "mocked-sensor" / "sensor.h"))
    assert "platform_read_sensor" in protos
    assert protos["platform_read_sensor"] == "int platform_read_sensor(uint16_t *reading);"


# --- inventory --------------------------------------------------------------


def test_requirements_extracted_verbatim_with_line_numbers():
    reqs = extract_requirements(EXAMPLES / "saturation" / "sdd.md")
    ids = [r.id for r in reqs]
    assert ids == ["SAT-001", "SAT-002", "SAT-003", "SAT-004"]
    assert reqs[0].statement.startswith("`saturate_u16")
    assert all(r.source_line > 0 for r in reqs)


def test_snapshot_hash_is_stable_and_content_addressed(tmp_path: Path):
    a = tmp_path / "a.c"
    a.write_text("int f(void){return 0;}\n")
    b = tmp_path / "b.c"
    b.write_text("int f(void){return 0;}\n")
    assert snapshot_hash([a]) == snapshot_hash([a])  # stable
    assert snapshot_hash([a]) != snapshot_hash([b])  # path participates
    b.write_text("int f(void){return 1;}\n")
    assert snapshot_hash([b]) != snapshot_hash([a])  # content participates


def test_inventory_reports_missing_include():
    inv = build_inventory(EXAMPLES, _selected("vendor-adapter"))
    unit = inv.units[0]
    assert unit.missing_includes == ["vendor_adc.h"]
    assert "vendor_adc_read" in unit.externals


# --- plan grounding ---------------------------------------------------------


def _inventory():
    return build_inventory(EXAMPLES, _selected("saturation"))


def test_specified_oracle_without_known_requirement_is_downgraded():
    inv = _inventory()
    response = (
        '{"scenarios": [{"id": "S1", "function": "saturate_u16", '
        '"requirement_ids": [], "technique": "t", "cases": [{"args": [1, 0], "expected": 1}], '
        '"oracle": {"kind": "SPECIFIED", "statement": "x", "rationale": "y"}}]}'
    )
    plan = parse_plan(response, inv, provider_identity())
    assert plan.scenarios[0].oracle.kind == UNDETERMINED
    assert plan.validation["downgraded"]


def test_scenario_with_unknown_function_is_rejected():
    inv = _inventory()
    response = (
        '{"scenarios": [{"id": "S1", "function": "not_real", "requirement_ids": ["SAT-001"], '
        '"cases": [], "oracle": {"kind": "SPECIFIED", "statement": "x"}}]}'
    )
    with pytest.raises(MtmError):
        parse_plan(response, inv, provider_identity())


def test_undetermined_scenario_has_expected_values_stripped():
    inv = _inventory()
    response = (
        '{"scenarios": [{"id": "S1", "function": "saturate_u16", "requirement_ids": [], '
        '"cases": [{"args": [1, 0], "expected": 5}], '
        '"oracle": {"kind": "UNDETERMINED", "statement": "x", "proposal": "clarify"}}]}'
    )
    plan = parse_plan(response, inv, provider_identity())
    assert plan.scenarios[0].cases[0].expected is None
    assert plan.scenarios[0].oracle.kind == UNDETERMINED


def test_undetermined_without_requirement_still_accepted():
    inv = _inventory()
    response = (
        '{"scenarios": [{"id": "S1", "function": "saturate_u16", "requirement_ids": [], '
        '"cases": [{"args": [1, 0]}], '
        '"oracle": {"kind": "UNDETERMINED", "statement": "x", "proposal": "p"}}]}'
    )
    plan = parse_plan(response, inv, provider_identity())
    assert plan.scenarios[0].oracle.is_undetermined


def test_prompt_contains_only_inventory_facts():
    inv = _inventory()
    prompt = build_prompt(inv)
    assert "saturate_u16" in prompt and "SAT-001" in prompt
    assert "expected-outcomes" not in prompt  # ground truth never leaked


def provider_identity():
    from mtm.llm import LLMIdentity

    return LLMIdentity(provider="test", model="test-model")


# --- provider ---------------------------------------------------------------


def test_mock_provider_selects_recording_by_fixture_facts():
    provider = MockProvider()
    import json

    text = provider.complete('{"units": [{"functions": [{"name": "saturate_u16"}]}], "requirements": [{"id": "SAT-001"}]}')
    payload = json.loads(text)
    assert payload["scenarios"]


def test_mock_provider_raises_without_a_match():
    provider = MockProvider()
    with pytest.raises(MtmError):
        provider.complete("no fixture facts here at all")


def test_make_provider_mock_is_token_free():
    assert isinstance(make_provider("mock", image_ref="img:1"), MockProvider)


def test_make_provider_auto_does_not_silently_fall_back_to_recordings(tmp_path):
    """B6: 'auto' must never substitute a recorded fixture for a real model call.

    With no built image it fails with a clear action rather than quietly
    returning canned answers that make a generated suite look model-authored.
    """
    with pytest.raises(MtmError) as exc:
        make_provider("auto", image_ref="never-built:0",
                      credentials_file=tmp_path / "absent.env")
    assert exc.value.code == "LLM_CONFIG"
    assert "mtm install" in (exc.value.action or "")


def test_make_provider_claude_code_requires_credentials(tmp_path):
    # With a nonexistent image, the image check fires first (LLM_CONFIG). To
    # exercise the credential branch, point at an image that exists.
    from mtm.docker import image_exists
    if not image_exists("img:1"):
        pytest.skip("no local image named img:1 to pass the image-existence check")
    with pytest.raises(MtmError) as exc:
        make_provider("claude-code", image_ref="img:1", credentials_file=tmp_path / "absent.env")
    assert exc.value.code == "LLM_AUTH"


def test_make_provider_unknown_name():
    with pytest.raises(MtmError):
        make_provider("nope", image_ref="img:1")


# --- generator: plans that cannot be materialised faithfully ----------------

from mtm.gen import _case_problem, _strip_c_comments, aggregate_aliases, mock_controls  # noqa: E402
from mtm.plan import TestCase as PlannedCase  # noqa: E402

_SHIM = """
typedef struct { unsigned short raw; } vendor_adc_sample_t;
int vendor_adc_read(uint8_t channel, vendor_adc_sample_t *sample);
"""


def test_aggregate_aliases_detected_from_typedef():
    assert "vendor_adc_sample_t" in aggregate_aliases(_SHIM)
    assert "uint8_t" not in aggregate_aliases(_SHIM)


def test_scalar_pointee_is_not_an_aggregate():
    ctrl = mock_controls("int platform_read_sensor(uint16_t *reading);",
                         "platform_read_sensor", aggregate_aliases(_SHIM))
    assert ctrl["out"] == ["reading"]
    assert ctrl["out_aggregate"] == set()


def test_aggregate_pointee_is_flagged_so_a_scalar_is_refused():
    """A scalar for a struct pointee would generate C that does not compile."""
    aggregates = aggregate_aliases(_SHIM)
    ctrl = mock_controls("int vendor_adc_read(uint8_t channel, vendor_adc_sample_t *sample);",
                         "vendor_adc_read", aggregates)
    assert ctrl["out_aggregate"] == {"sample"}
    scalar = PlannedCase(mocks={"vendor_adc_read": {"ret": 0, "sample": 1234}})
    problem = _case_problem(scalar, {"vendor_adc_read": ctrl}, [], aggregates)
    assert problem and "aggregate" in problem


def test_aggregate_pointee_accepts_an_explicit_c_initializer():
    aggregates = aggregate_aliases(_SHIM)
    ctrl = mock_controls("int vendor_adc_read(uint8_t channel, vendor_adc_sample_t *sample);",
                         "vendor_adc_read", aggregates)
    good = PlannedCase(mocks={"vendor_adc_read": {"ret": 0,
                                               "sample": {"expr": "(vendor_adc_sample_t){7}"}}})
    assert _case_problem(good, {"vendor_adc_read": ctrl}, [], aggregates) is None


def test_unsupported_mock_control_is_refused_not_silently_dropped():
    """An unknown control must be a recorded limitation, never a default-driven test."""
    ctrl = mock_controls("int platform_read_sensor(uint16_t *reading);",
                         "platform_read_sensor")
    case = PlannedCase(mocks={"platform_read_sensor": {"call_count": 1}})
    problem = _case_problem(case, {"platform_read_sensor": ctrl}, [])
    assert problem and "call_count" in problem


def test_expect_out_index_must_be_a_pointer_parameter():
    ctrl = mock_controls("int platform_read_sensor(uint16_t *reading);",
                         "platform_read_sensor")
    from mtm.gen import CParam
    params = [CParam(type="uint16_t", name="reading", pointer=True)]
    assert _case_problem(PlannedCase(expect_out={"0": 5}), {"x": ctrl}, params) is None
    assert _case_problem(PlannedCase(expect_out={"7": 5}), {"x": ctrl}, params)
    assert _case_problem(PlannedCase(expect_out={"reading": 5}), {"x": ctrl}, params)


def test_declaration_comments_are_stripped_before_parsing():
    decl = "/* ASSUMED: signature not documented */\nint f(uint16_t *p);"
    assert _strip_c_comments(decl).strip() == "int f(uint16_t *p);"
    assert mock_controls(decl, "f") is not None


# --- B4: an UNDETERMINED scenario never asserts an ungrounded outcome --------


def _undetermined_plan(payload: dict):
    import json as _json
    from mtm.llm import LLMIdentity
    from mtm.inventory import build_inventory

    inventory = build_inventory(EXAMPLES, _selected("mocked-sensor"))
    return parse_plan(_json.dumps(payload), inventory,
                      LLMIdentity(provider="test", model="hand-written"))


def test_undetermined_strips_expect_out_so_it_cannot_assert():
    """An UNDETERMINED case with a null `expected` but a grounded-looking
    `expect_out` must not keep that outcome: it would assert a value the SDD
    never states and report a false implementation defect (B4)."""
    plan = _undetermined_plan({
        "scenarios": [{
            "id": "S1", "title": "t", "function": "sample_sensor",
            "requirement_ids": ["SEN-001"], "mocks": ["platform_read_sensor"],
            "cases": [{"args": [1], "expected": None,
                       "mocks": {"platform_read_sensor": {"ret": 0, "reading": 5}},
                       "expect_out": {"0": 42}, "expect_calls": {"platform_read_sensor": 3}}],
            "oracle": {"kind": "UNDETERMINED", "statement": "gap", "proposal": "specify"},
        }],
    })
    case = plan.scenarios[0].cases[0]
    assert case.expected is None
    assert case.expect_out == {}
    assert case.expect_calls == {}
    assert plan.scenarios[0].oracle.kind == UNDETERMINED


def test_specified_scenario_keeps_its_grounded_expectations():
    """Contrast: a SPECIFIED oracle keeps its expectations (no over-stripping)."""
    plan = _undetermined_plan({
        "scenarios": [{
            "id": "S1", "title": "t", "function": "sample_sensor",
            "requirement_ids": ["SEN-002"], "mocks": ["platform_read_sensor"],
            "cases": [{"args": [642], "expected": "SENSOR_ERROR",
                       "mocks": {"platform_read_sensor": {"ret": 1, "reading": 55}},
                       "expect_out": {"0": 642}, "expect_calls": {"platform_read_sensor": 1}}],
            "oracle": {"kind": "SPECIFIED", "statement": "x", "rationale": "y"},
        }],
    })
    case = plan.scenarios[0].cases[0]
    assert case.expected == "SENSOR_ERROR"
    assert case.expect_out == {"0": 642}
    assert case.expect_calls == {"platform_read_sensor": 1}
