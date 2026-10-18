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

    assert (frozen_suite_hash(correct)["tests"]
            == frozen_suite_hash(faulty)["tests"]), "the frozen tests must be unchanged"

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
