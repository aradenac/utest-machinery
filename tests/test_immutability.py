"""Input-immutability tests: selected originals never change (SYS-004)."""

from __future__ import annotations

import hashlib
from pathlib import Path

from conftest import REPO, run_mtm_json

EXAMPLES = REPO / "examples"


def _hashes(root: Path) -> dict[str, str]:
    result = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            result[str(path.relative_to(root))] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def test_selected_sources_unchanged_by_scope_operations(bench: Path):
    source = EXAMPLES / "saturation"
    before = _hashes(source)
    run_mtm_json("scope", "add", str(source), "--kind", "source", cwd=bench)
    run_mtm_json("scope", "list", cwd=bench)
    run_mtm_json("status", cwd=bench)
    run_mtm_json("doctor", cwd=bench)
    after = _hashes(source)
    assert before == after, "selected originals were modified"


def test_sdd_fixture_unchanged(bench: Path):
    sdd = EXAMPLES / "safety-decision" / "sdd.md"
    before = hashlib.sha256(sdd.read_bytes()).hexdigest()
    run_mtm_json("scope", "add", str(sdd), "--kind", "spec", cwd=bench)
    after = hashlib.sha256(sdd.read_bytes()).hexdigest()
    assert before == after
