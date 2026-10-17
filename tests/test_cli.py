"""CLI contract tests: init, scope, status, doctor, and the unimplemented gate."""

from __future__ import annotations

import json
import os
from pathlib import Path

import jsonschema
import pytest
import yaml

from conftest import REPO, run_mtm, run_mtm_json

SCHEMAS = REPO / "mtm" / "schemas"


def test_init_creates_bench(bench: Path):
    assert (bench / "project.yaml").is_file()
    config = yaml.safe_load((bench / "project.yaml").read_text())
    assert config["schema_version"] == 1
    assert config["bench_id"].startswith("bench-")
    assert (bench / ".mtm" / "state").is_dir()


def test_init_is_idempotent_guarded(bench: Path):
    record, code = run_mtm_json("init", str(bench))
    assert code == 40  # BENCH_EXISTS -> invalid input
    assert record["code"] == "BENCH_EXISTS"
    assert record["action"]


def test_init_missing_dir_without_flag(tmp_path: Path):
    record, code = run_mtm_json("init", str(tmp_path / "nope"))
    assert code == 40
    assert record["code"] == "INPUT_MISSING"


def test_project_config_validates_against_schema(bench: Path):
    schema = json.loads((SCHEMAS / "project.schema.json").read_text())
    config = yaml.safe_load((bench / "project.yaml").read_text())
    jsonschema.validate(config, schema)


def test_status_outside_bench_reports_no_bench(tmp_path: Path):
    record, code = run_mtm_json("status", cwd=tmp_path)
    assert code == 0
    assert record["status"] == "NO_BENCH"
    assert record["bench_root"] is None


def test_status_inside_bench(bench: Path):
    record, code = run_mtm_json("status", cwd=bench)
    assert code == 0
    assert record["bench_id"].startswith("bench-")


def test_scope_add_list_remove_roundtrip(bench: Path, tmp_path: Path):
    target = tmp_path / "mod.c"
    target.write_text("int f(void){return 0;}\n")
    add, code = run_mtm_json("scope", "add", str(target), "--kind", "source", cwd=bench)
    assert code == 0
    scope_id = add["scope"]["id"]
    assert add["scope"]["sha256"]

    listed, code = run_mtm_json("scope", "list", cwd=bench)
    assert code == 0
    assert [s["id"] for s in listed["scopes"]] == [scope_id]

    removed, code = run_mtm_json("scope", "remove", scope_id, cwd=bench)
    assert code == 0
    assert removed["previous_count"] == 1
    assert removed["scope_count"] == 0
    assert target.is_file()  # nothing on disk is deleted


def test_scope_add_missing_path(bench: Path):
    record, code = run_mtm_json("scope", "add", "/does/not/exist.c", cwd=bench)
    assert code == 40
    assert record["code"] == "INPUT_MISSING"


def test_scope_add_duplicate_rejected(bench: Path, tmp_path: Path):
    target = tmp_path / "mod.c"
    target.write_text("int f(void){return 0;}\n")
    run_mtm_json("scope", "add", str(target), cwd=bench)
    record, code = run_mtm_json("scope", "add", str(target), cwd=bench)
    assert code == 40
    assert record["code"] == "SCOPE_INVALID"


def test_scope_remove_unknown(bench: Path):
    record, code = run_mtm_json("scope", "remove", "scope-deadbeef", cwd=bench)
    assert code == 40
    assert record["code"] == "SCOPE_INVALID"


def test_last_operation_journal_written(bench: Path, tmp_path: Path):
    run_mtm_json("scope", "add", "/nope.c", cwd=bench)  # a failing op still journals
    journal = bench / ".mtm" / "state" / "last-operation.json"
    assert journal.is_file()
    record = json.loads(journal.read_text())
    schema = json.loads((SCHEMAS / "operation-status.schema.json").read_text())
    jsonschema.validate(record, schema)
    assert record["status"] == "FAILED"


def test_doctor_reports_credentials_error_when_absent(bench: Path, tmp_path: Path):
    # Point HOME at an empty dir so ~/.config/utest-machinery is absent.
    record = json.loads(run_mtm("--json", "doctor", cwd=bench, env={"HOME": str(tmp_path)}).stdout)
    creds = [c for c in record["checks"] if c["name"] == "credentials"]
    assert creds and not creds[0]["ok"]
    assert creds[0]["level"] == "error"


def test_doctor_credentials_ok_with_600_file(bench: Path, tmp_path: Path):
    cfg = tmp_path / ".config" / "utest-machinery"
    cfg.mkdir(parents=True)
    creds = cfg / "credentials.env"
    creds.write_text("SCW_BASE_URL=https://example.invalid\nSCW_BEARER_TOKEN=secret\n")
    os.chmod(creds, 0o600)
    record = json.loads(run_mtm("--json", "doctor", cwd=bench, env={"HOME": str(tmp_path)}).stdout)
    entry = [c for c in record["checks"] if c["name"] == "credentials"][0]
    assert entry["ok"], entry


def test_doctor_flags_world_readable_credentials(bench: Path, tmp_path: Path):
    cfg = tmp_path / ".config" / "utest-machinery"
    cfg.mkdir(parents=True)
    creds = cfg / "credentials.env"
    creds.write_text("SCW_BASE_URL=x\nSCW_BEARER_TOKEN=y\n")
    os.chmod(creds, 0o644)
    record = json.loads(run_mtm("--json", "doctor", cwd=bench, env={"HOME": str(tmp_path)}).stdout)
    entry = [c for c in record["checks"] if c["name"] == "credentials"][0]
    assert not entry["ok"]
    assert "600" in entry["action"]


@pytest.mark.parametrize("command", ["plan", "generate", "verify", "interpret", "report"])
def test_unimplemented_commands_are_explicit(command: str, bench: Path):
    record, code = run_mtm_json(command, cwd=bench)
    assert code == 40
    assert record["code"] == "NOT_IMPLEMENTED"
    assert "later milestone" in record["action"] or "PR" in record["action"]


def test_bench_flag_switches_directory(bench: Path, tmp_path: Path):
    record, code = run_mtm_json("--bench", str(bench), "status", cwd=tmp_path)
    assert code == 0
    assert record["bench_root"] == str(bench)


def test_status_does_not_overwrite_last_operation(bench: Path):
    """A failed operation must survive repeated 'mtm status' consultations."""
    # Cause a failing operation that journals.
    run_mtm_json("scope", "add", "/does/not/exist.c", cwd=bench)
    journal = bench / ".mtm" / "state" / "last-operation.json"
    first = json.loads(journal.read_text())
    assert first["status"] == "FAILED"

    run_mtm_json("status", cwd=bench)
    second = json.loads(journal.read_text())
    run_mtm_json("status", cwd=bench)
    third = json.loads(journal.read_text())

    assert second["code"] == first["code"] == "INPUT_MISSING"
    assert third["code"] == "INPUT_MISSING"
    assert third["message"]


def test_status_text_shows_last_failure(bench: Path):
    """Human 'mtm status' must render the last operation's diagnosis, not only JSON."""
    run_mtm_json("scope", "add", "/does/not/exist.c", cwd=bench)
    proc = run_mtm("status", cwd=bench)
    assert proc.returncode == 0
    combined = proc.stdout + proc.stderr
    assert "last operation" in combined
    assert "INPUT_MISSING" in combined
    assert "next:" in combined  # the actionable step is rendered


def test_status_text_shows_diagnosis_after_two_queries(bench: Path):
    run_mtm_json("scope", "add", "/does/not/exist.c", cwd=bench)
    run_mtm("status", cwd=bench)
    second = run_mtm("status", cwd=bench)
    assert "INPUT_MISSING" in (second.stdout + second.stderr)


def test_init_force_preserves_scopes(bench: Path, tmp_path: Path):
    target = tmp_path / "mod.c"
    target.write_text("int f(void){return 0;}\n")
    added, _ = run_mtm_json("scope", "add", str(target), cwd=bench)
    scope_id = added["scope"]["id"]

    record, code = run_mtm_json("init", str(bench), "--force", cwd=bench)
    assert code == 0, record

    listed, _ = run_mtm_json("scope", "list", cwd=bench)
    assert [s["id"] for s in listed["scopes"]] == [scope_id]


def test_init_force_preserves_selectors_and_unknown_fields(bench: Path):
    """--force must not silently drop persisted settings or additive fields."""
    project = bench / "project.yaml"
    config = yaml.safe_load(project.read_text())
    config["selectors"] = {"unit": "adc"}
    config["future_field"] = {"kept": True}
    project.write_text(yaml.safe_dump(config, sort_keys=False))

    record, code = run_mtm_json("init", str(bench), "--force", cwd=bench)
    assert code == 0, record

    after = yaml.safe_load(project.read_text())
    assert after["selectors"] == {"unit": "adc"}
    assert after["future_field"] == {"kept": True}



def test_scope_add_rejects_schema_incompatible_id(bench: Path, tmp_path: Path):
    target = tmp_path / "mod.c"
    target.write_text("int f(void){return 0;}\n")
    record, code = run_mtm_json("scope", "add", str(target), "--id", "not-a-scope-id", cwd=bench)
    assert code == 40
    assert record["code"] == "SCOPE_INVALID"


def test_corrupt_config_fails_schema_validation(tmp_path: Path):
    bad = tmp_path / "bad"
    bad.mkdir()
    (bad / "project.yaml").write_text("schema_version: 1\nbench_id: x\nscopes: [{id: 1}]\n")
    record, code = run_mtm_json("status", cwd=bad)
    assert code == 40
    assert record["code"] == "CONFIG_INVALID"


def test_unknown_config_field_is_not_rejected(tmp_path: Path):
    bench_dir = tmp_path / "b2"
    bench_dir.mkdir()
    (bench_dir / "project.yaml").write_text(
        "schema_version: 1\nbench_id: bench-x\nfuture_field: hello\n"
    )
    record, code = run_mtm_json("status", cwd=bench_dir)
    assert code == 0
    assert record["status"] == "SUCCEEDED"


def test_invalid_bench_flag(bench: Path, tmp_path: Path):
    record, code = run_mtm_json("--bench", str(tmp_path / "nope"), "status")
    assert code == 40
    assert record["code"] == "CONFIG_INVALID"


def test_human_output_has_next_action_on_error(bench: Path):
    proc = run_mtm("scope", "remove", "scope-0000", cwd=bench)
    assert proc.returncode == 40
    assert "next:" in proc.stderr


def test_image_reference_helpers_use_full_ref():
    """Regression: image_exists/image_digest take a reference, not a bare tag."""
    from mtm.docker import image_ref

    assert image_ref("1.0.0") == "utest-machinery/mtm:1.0.0"


def test_doctor_image_check_uses_full_reference(monkeypatch):
    """Regression: doctor must pass the full image reference to image_exists."""
    import mtm.commands as commands
    from mtm.commands import Context, _run_checks

    seen: list[str] = []

    def fake_exists(ref):
        seen.append(ref)
        return True

    monkeypatch.setattr(commands, "image_exists", fake_exists)
    monkeypatch.setattr(commands, "docker_available",
                        lambda: __import__("mtm.docker", fromlist=["DockerInfo"]).DockerInfo(True, "test"))
    monkeypatch.setattr(commands, "image_digest", lambda ref: "sha256:test")
    ctx = Context(context=str(REPO), image_tag="1.0.0")
    _run_checks(None, ctx)
    assert seen == ["utest-machinery/mtm:1.0.0"]
