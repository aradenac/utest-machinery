"""CLI contract tests: init, scope, status, doctor, and the unimplemented gate."""

from __future__ import annotations

import json
import os
from pathlib import Path

import jsonschema
import pytest
import yaml

from conftest import REPO, run_mtm, run_mtm_json

SCHEMAS = REPO / "schemas"


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


def test_invalid_bench_flag(bench: Path, tmp_path: Path):
    record, code = run_mtm_json("--bench", str(tmp_path / "nope"), "status")
    assert code == 40
    assert record["code"] == "CONFIG_INVALID"


def test_human_output_has_next_action_on_error(bench: Path):
    proc = run_mtm("scope", "remove", "scope-0000", cwd=bench)
    assert proc.returncode == 40
    assert "next:" in proc.stderr
