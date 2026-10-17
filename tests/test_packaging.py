"""Packaging test: the CLI must work when installed, not only from the checkout.

Builds a wheel, installs it into a throwaway virtual environment, and runs the
CLI from outside the source tree. This catches package-data gaps such as a schema
or bundled DAG that is only found in the checkout, and pins the **distribution
contract**: a wheel ships the control commands and their data, while building the
generic Docker image requires the source tree (a wheel does not carry a Docker
context, and must not silently pretend to).

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


@pytest.fixture(scope="module")
def installed_cli(tmp_path_factory) -> Path:
    """Build the wheel once, install it in a clean venv, return that interpreter."""
    tmp = tmp_path_factory.mktemp("packaging")
    py = _bootstrap_python(tmp)

    wheel_dir = tmp / "wheel"
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

    # Install into a clean venv (let pip resolve runtime deps); skip when offline.
    env_dir = tmp / "venv"
    venv.create(env_dir, with_pip=True)
    app_py = env_dir / "bin" / "python"
    install = subprocess.run(
        [str(app_py), "-m", "pip", "install", str(wheels[0])],
        capture_output=True, text=True,
    )
    if install.returncode != 0:
        pytest.skip(f"install of the wheel failed (offline?): {install.stderr.strip()[-300:]}")
    return app_py


def test_installed_cli_validates_and_has_package_data(installed_cli: Path, tmp_path: Path):
    """The installed CLI finds its bundled schema from outside the checkout."""
    bench = tmp_path / "bench"
    bench.mkdir()
    run = subprocess.run(
        [str(installed_cli), "-m", "mtm.cli", "init", str(bench)],
        capture_output=True, text=True, cwd=str(tmp_path),
    )
    assert run.returncode == 0, run.stderr
    run = subprocess.run(
        [str(installed_cli), "-m", "mtm.cli", "status"],
        capture_output=True, text=True, cwd=str(bench),
    )
    assert run.returncode == 0, run.stderr


def test_installed_cli_image_install_requires_source_context(installed_cli: Path, tmp_path: Path):
    """Distribution contract: a wheel cannot build the image without a source tree.

    `mtm install` from outside a checkout must fail with an actionable message
    (exit 40, CONFIG_INVALID) — never silently succeed or crash obscurely.
    """
    run = subprocess.run(
        [str(installed_cli), "-m", "mtm.cli", "--json", "install"],
        capture_output=True, text=True, cwd=str(tmp_path),
    )
    assert run.returncode == 40, (run.returncode, run.stdout, run.stderr)
    import json

    record = json.loads(run.stdout)
    assert record["code"] == "CONFIG_INVALID"
    assert "source" in record["action"].lower()
