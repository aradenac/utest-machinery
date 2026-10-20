"""PR 3 execution-tier tests: real profiles and the R22 fixture campaign.

These need the product image (Ceedling + Clang/LLVM + Valgrind) and are skipped in
the fast matrix. They are the **real compile/run** evidence the PR 3 DoD requires
(§A: the generated project compiles and links in the image and asserts on a real
SUT; DoD 3: a reference-correct SUT passes and a genuinely faulty SUT fails).

The R22 campaign runs the **same unchanged frozen tests/oracles** against two
separately snapshotted SUTs (correct and faulty) — never swapping the input of a
single frozen campaign (R22 step 2).
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from conftest import REPO, run_mtm_json

pytestmark = pytest.mark.docker

EXAMPLES = REPO / "examples"


def _init_bench(dst: Path, fixture: str) -> list[dict]:
    dst.mkdir()
    for source in sorted((EXAMPLES / fixture).iterdir()):
        if source.is_file():
            shutil.copy(source, dst / source.name)
    record, code = run_mtm_json("init", str(dst))
    assert code == 0, record
    for source in sorted(dst.iterdir()):
        if source.suffix in (".c", ".h", ".md"):
            kind = "source" if source.suffix in (".c", ".h") else "spec"
            run_mtm_json("scope", "add", source.name, "--kind", kind, cwd=dst)
    import yaml

    return yaml.safe_load((dst / "project.yaml").read_text())["scopes"]


def _generate(bench: Path, image_ref: str) -> dict:
    record, code = run_mtm_json("--image-tag", image_ref.split(":")[-1],
                                "generate", "--provider", "mock", cwd=bench)
    assert code in (0, 10), record  # 10 = a finding, still a produced suite
    return record


def test_all_profiles_execute_against_the_product_image(tmp_path, product_image):
    """DoD 2: functional, coverage/MC-DC, sanitizers, clang -O2 and Valgrind all
    execute in the product image with native reports, and no target-MCU claim."""
    bench = tmp_path / "correct"
    scopes = _init_bench(bench, "qualification/saturation-correct")
    _generate(bench, product_image)
    record, code = run_mtm_json("--image-tag", product_image.split(":")[-1],
                                "verify", cwd=bench)
    assert code == 0, record
    by_id = {p["id"]: p for p in record["profiles"]}
    for pid in ("functional", "coverage", "sanitizers", "compiler-clang", "valgrind"):
        assert by_id[pid]["status"] in ("PASSED", "FAILED"), (pid, by_id[pid])
    # Coverage came from a real instrumented run with a truthful denominator.
    cov = by_id["coverage"]["metrics"]
    assert cov["lines"]["count"] > 0 and cov["lines"]["covered"] > 0
    assert "framework" in cov  # vendored framework kept out of the headline figure
    # The functional profile reports the real Unity totals.
    assert by_id["functional"]["metrics"]["tests"] >= 1
    status = json.loads((bench / ".mtm" / "state" / "verify"
                         / record["run_id"] / "status.json").read_text())
    assert "no target-MCU claim" in status["note"]


def test_same_frozen_suite_passes_correct_and_detects_the_faulty_sut(tmp_path, product_image):
    """DoD 3 + R22: one frozen suite, two independently snapshotted SUTs — the
    reference-correct variant PASSES and the faulty variant FAILS a relevant
    assertion. The frozen test bytes are identical across both campaigns."""
    from mtm.inventory import resolve_scope_paths, snapshot_hash
    from mtm.paths import write_json_atomic
    from mtm.qualify import DETECTED, classify_defect, frozen_suite_hash

    tag = product_image.split(":")[-1]
    correct = tmp_path / "correct"
    cscopes = _init_bench(correct, "qualification/saturation-correct")
    _generate(correct, product_image)
    rc, code = run_mtm_json("--image-tag", tag, "verify", cwd=correct)
    assert code == 0, rc

    # A separate snapshot (copied bench) whose SUT is the seeded-faulty variant;
    # the generated tests/oracles are carried over byte-identical.
    faulty = tmp_path / "faulty"
    shutil.copytree(correct, faulty)
    faulty_src = (EXAMPLES / "saturation" / "saturation.c").read_text()
    (faulty / "saturation.c").write_text(faulty_src)
    (faulty / ".mtm" / "gen" / "src" / "saturation.c").write_text(faulty_src)
    fscopes = [s for s in _scopes_of(faulty)]
    new_hash = snapshot_hash(resolve_scope_paths(faulty, fscopes))
    for name in ("inventory.json", "plan.json"):
        path = faulty / ".mtm" / "state" / "generate" / name
        data = json.loads(path.read_text())
        data["snapshot_hash"] = new_hash
        write_json_atomic(path, data)

    # The cloned faulty variant is a *different frozen SUT identity*, not a
    # mutation replayed under the correct variant's authenticated fingerprint.
    # Re-seal this independent fixture after the deliberate SUT substitution;
    # test/oracle bytes stay identical, copied executable-source hashes do not.
    plan_path = faulty / ".mtm" / "state" / "generate" / "plan.json"
    plan_data = json.loads(plan_path.read_text())
    image = plan_data["suite_hash"]["image"]
    plan_data["suite_hash"] = frozen_suite_hash(faulty)
    plan_data["suite_hash"]["image"] = image
    write_json_atomic(plan_path, plan_data)
    assert (frozen_suite_hash(correct)["tests"]
            == frozen_suite_hash(faulty)["tests"]), "the frozen tests must be unchanged"
    assert (frozen_suite_hash(correct)["sources"]
            != frozen_suite_hash(faulty)["sources"]), "the SUT variant must differ"

    rf, code = run_mtm_json("--image-tag", tag, "verify", cwd=faulty)
    assert code == 10, rf  # a real finding
    assert rf["status"] == "FINDINGS"
    failing = {p["id"]: p["status"] for p in rf["profiles"]}
    result, _ = classify_defect(correct_status="PASSED",
                                faulty_status=failing["functional"])
    assert result == DETECTED
    assert any(f["category"] == "IMPLEMENTATION_DEFECT" for f in rf["findings"])


def test_a_stale_snapshot_is_refused_through_the_cli(tmp_path, product_image):
    """DoD 1 (real CLI): editing a selected input after freezing makes ``verify``
    return ``SNAPSHOT_MISMATCH`` (exit 40) and run nothing."""
    bench = tmp_path / "stale"
    _init_bench(bench, "qualification/saturation-correct")
    _generate(bench, product_image)
    (bench / "saturation.c").write_text("int unrelated(void) { return 0; }\n")
    record, code = run_mtm_json("--image-tag", product_image.split(":")[-1],
                                "verify", cwd=bench)
    assert code == 40, record
    assert record["code"] == "SNAPSHOT_MISMATCH"


def _scopes_of(bench: Path) -> list[dict]:
    import yaml

    return yaml.safe_load((bench / "project.yaml").read_text())["scopes"]



def test_conditional_mock_uses_selected_macro_and_native_ceedling_preprocessor(
        tmp_path, product_image):
    """DoD B/C: real Clang + CMock must compile a macro-selected interface.

    Without FEATURE_SENSOR the selected header does not declare the mockable
    symbol. The generator must preserve that context in both native tools.
    """
    from mtm.ceedling import run_ceedling
    from mtm.gen import generate_suite
    from mtm.inventory import build_inventory
    from mtm.llm import LLMIdentity
    from mtm.plan import parse_plan

    bench = tmp_path / "conditional-mock"
    bench.mkdir()
    (bench / "include").mkdir()
    (bench / "include" / "adapter.h").write_text(
        "#ifndef ADAPTER_H\n#define ADAPTER_H\n"
        "#ifdef FEATURE_SENSOR\nint platform_value(int x);\n#endif\n"
        "#endif\n")
    (bench / "sensor.c").write_text(
        '#include "adapter.h"\n'
        "int sensor_read(int x) { return platform_value(x); }\n")
    (bench / "sdd.md").write_text(
        "# SDD\n- **SEN-001**: sensor_read SHALL return the dependency's value.\n")
    run_mtm_json("init", str(bench))
    for name, kind in (("sensor.c", "source"), ("include", "include"),
                       ("sdd.md", "spec")):
        _, code = run_mtm_json("scope", "add", name, "--kind", kind, cwd=bench)
        assert code == 0
    scopes = _scopes_of(bench)
    inventory = build_inventory(bench, scopes)
    plan = parse_plan(json.dumps({"scenarios": [{
        "id": "S1", "title": "macro mock", "function": "sensor_read",
        "requirement_ids": ["SEN-001"], "mocks": ["platform_value"],
        "cases": [{"args": [2], "expected": 7,
                   "mocks": {"platform_value": {"ret": 7}}}],
        "oracle": {"kind": "SPECIFIED", "statement": "returns dependency value",
                   "rationale": "SEN-001"}}]}), inventory,
        LLMIdentity(provider="test", model="t"))
    context = {"include_dirs": ["include"], "defines": ["FEATURE_SENSOR=1"],
               "mock_preprocessor": "mocks"}
    out = generate_suite(bench, plan, inventory, image=product_image,
                         build_context=context)
    assert out.test_functions, out.limitations
    assert out.types["declaration_sources"]["platform_value"] == "clang"
    project = (bench / ".mtm" / "gen" / "project.yml").read_text()
    assert ":use_test_preprocessor: :mocks" in project
    assert "FEATURE_SENSOR=1" in project
    run = run_ceedling(bench, image_ref=product_image)
    assert run.ran and run.failed == 0 and run.total >= 1, run.diagnostics
