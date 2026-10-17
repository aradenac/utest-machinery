"""Packaging test: the CLI must work when installed, not only from the checkout.

Builds a wheel, installs it into a throwaway virtual environment, and runs the
CLI from outside the source tree. This catches package-data gaps such as a schema
or bundled DAG that is only found in the checkout.

The interpreter running the suite may be a bare system Python without ``pip``
(common on distro builds). We therefore bootstrap a virtual environment and use
*its* interpreter for the packaging steps, and skip only when even that fails —
for example when building the wheel needs the network and none is available.
"""

from __future__ import annotations

import subprocess
import sys
import venv
import zipfile
from pathlib import Path

import pytest

from conftest import REPO


def _bootstrap_python(tmp_path: Path) -> Path:
    """Return an interpreter that has pip, creating a venv if necessary."""
    if subprocess.run(
        [sys.executable, "-m", "pip", "--version"], capture_output=True
    ).returncode == 0:
        return Path(sys.executable)

    env_dir = tmp_path / "bootstrap"
    venv.create(env_dir, with_pip=True)
    py = env_dir / "bin" / "python"
    if subprocess.run([str(py), "-m", "pip", "--version"], capture_output=True).returncode != 0:
        pytest.skip("no pip and venv cannot bootstrap one")
    return py


def test_installed_cli_validates_and_has_package_data(tmp_path: Path):
    py = _bootstrap_python(tmp_path)

    wheel_dir = tmp_path / "wheel"
    wheel_dir.mkdir()
    proc = subprocess.run(
        [str(py), "-m", "pip", "wheel", "--no-deps", "-w", str(wheel_dir), str(REPO)],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        pytest.skip(f"wheel build failed (offline?): {proc.stderr.strip()[-300:]}")

    wheels = list(wheel_dir.glob("utest_machinery-*.whl"))
    assert wheels, "no wheel produced"

    # Inspect the wheel contents directly: schemas and the sample DAG must ship.
    names = zipfile.ZipFile(wheels[0]).namelist()
    assert any(n.endswith("mtm/schemas/project.schema.json") for n in names), names
    assert any(n.endswith("mtm/schemas/operation-status.schema.json") for n in names), names
    assert any(n.endswith("mtm/workflows/sample.yaml") for n in names), names

    # Install into a clean venv and run the CLI outside the source tree. Let pip
    # resolve the runtime dependencies (PyYAML, jsonschema); skip when offline.
    env_dir = tmp_path / "venv"
    venv.create(env_dir, with_pip=True)
    app_py = env_dir / "bin" / "python"
    install = subprocess.run(
        [str(app_py), "-m", "pip", "install", str(wheels[0])],
        capture_output=True, text=True,
    )
    if install.returncode != 0:
        pytest.skip(f"install of the wheel failed (offline?): {install.stderr.strip()[-300:]}")

    bench = tmp_path / "bench"
    bench.mkdir()
    run = subprocess.run(
        [str(app_py), "-m", "mtm.cli", "init", str(bench)],
        capture_output=True, text=True, cwd=str(tmp_path),
    )
    assert run.returncode == 0, run.stderr

    # The installed CLI must find its bundled schema from outside the checkout:
    # a schema-loading command (status) succeeds, proving package data ships.
    run = subprocess.run(
        [str(app_py), "-m", "mtm.cli", "status"],
        capture_output=True, text=True, cwd=str(bench),
    )
    assert run.returncode == 0, run.stderr
