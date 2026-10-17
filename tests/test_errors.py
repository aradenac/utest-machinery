"""Unit tests for the error/exit contract (offline, deterministic)."""

from __future__ import annotations

from mtm.errors import (
    CONTRACT,
    EXIT_INTERRUPTED,
    EXIT_INVALID_INPUT,
    EXIT_OK,
    EXIT_PARTIAL,
    EXIT_TEST_FINDINGS,
    EXIT_TOOL_FAILURE,
    Code,
    MtmError,
    most_severe,
)


def test_exit_codes_match_contract():
    assert {EXIT_OK, EXIT_TEST_FINDINGS, EXIT_PARTIAL, EXIT_TOOL_FAILURE,
            EXIT_INVALID_INPUT, EXIT_INTERRUPTED} == {0, 10, 20, 30, 40, 130}


def test_most_severe_picks_highest_blocking():
    assert most_severe([0]) == EXIT_OK
    assert most_severe([EXIT_TEST_FINDINGS, EXIT_PARTIAL]) == EXIT_PARTIAL
    assert most_severe([EXIT_PARTIAL, EXIT_TOOL_FAILURE]) == EXIT_TOOL_FAILURE
    assert most_severe([]) == EXIT_OK


def test_every_known_code_has_a_contract_entry():
    codes = [v for k, v in vars(Code).items() if not k.startswith("_")]
    for code in codes:
        assert code in CONTRACT, f"{code} missing from CONTRACT"


def test_bench_missing_is_configuration_invalid_input():
    err = MtmError(code=Code.BENCH_MISSING, message="x")
    assert err.exit_code == EXIT_INVALID_INPUT
    assert err.category == "CONFIGURATION"


def test_interrupted_maps_to_130():
    assert MtmError(code=Code.WORKFLOW_INTERRUPTED, message="x").exit_code == EXIT_INTERRUPTED
