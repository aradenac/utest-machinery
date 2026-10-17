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

import os
import socket
import subprocess
import sys
import venv
import zipfile
from pathlib import Path

import pytest

from conftest import REPO


def _network_available() -> bool:
    """Whether PyPI is reachable — the only prerequisite that justifies a skip."""
    if os.environ.get("MTM_PACKAGING_OFFLINE") == "1":
        return False
    try:
        with socket.create_connection(("pypi.org", 443), timeout=5):
            return True
    except OSError:
        return False


def _skip_or_fail(what: str, detail: str, *, network: bool) -> None:
    """A build/install failure is a real regression when it could have worked.

    Offline (no PyPI) is the only verifiable unavailable prerequisite, so only
    that is skippable. In CI, or whenever the network is reachable, a failure is
    a genuine packaging defect and must fail loudly — never a silent green.
    """
    if network or os.environ.get("CI"):
        pytest.fail(f"{what} failed but the environment supports it: {detail[-500:]}")
    pytest.skip(f"{what} skipped: PyPI unreachable (offline prerequisite): {detail[-200:]}")


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
        pytest.skip("no pip available to run the packaging test")
    return py


@pytest.fixture(scope="module")
def installed_cli(tmp_path_factory) -> Path:
    """Build the wheel once, install it in a clean venv, return that interpreter."""
    tmp = tmp_path_factory.mktemp("packaging")
    network = _network_available()
    py = _bootstrap_python(tmp)

    wheel_dir = tmp / "wheel"
    wheel_dir.mkdir()
    proc = subprocess.run(
        [str(py), "-m", "pip", "wheel", "--no-deps", "-w", str(wheel_dir), str(REPO)],
        capture_output=True, text=True,
    )
    if proc.returncode != 0:
        _skip_or_fail("wheel build", proc.stderr, network=network)

    wheels = list(wheel_dir.glob("utest_machinery-*.whl"))
    assert wheels, "no wheel produced"

    # Inspect the wheel contents directly: schemas, the sample DAG and the image
    # build context must all ship (the context makes `mtm install` autonomous).
    names = zipfile.ZipFile(wheels[0]).namelist()
    assert any(n.endswith("mtm/schemas/project.schema.json") for n in names), names
    assert any(n.endswith("mtm/schemas/operation-status.schema.json") for n in names), names
    assert any(n.endswith("mtm/workflows/sample.yaml") for n in names), names
    assert any(n.endswith("mtm/image/Dockerfile") for n in names), names
    assert any(n.endswith("mtm/image/entrypoint.sh") for n in names), names
    assert any(n.endswith("mtm/image/toolchain.lock") for n in names), names

    # Install into a clean venv (let pip resolve runtime deps: PyYAML, jsonschema).
    env_dir = tmp / "venv"
    venv.create(env_dir, with_pip=True)
    app_py = env_dir / "bin" / "python"
    install = subprocess.run(
        [str(app_py), "-m", "pip", "install", str(wheels[0])],
        capture_output=True, text=True,
    )
    if install.returncode != 0:
        _skip_or_fail("wheel install", install.stderr, network=network)
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


def test_installed_cli_image_context_is_packaged(installed_cli: Path, tmp_path: Path):
    """The installed CLI carries its image build context (autonomous install).

    `mtm install` assembles the context from the package, so the build context
    files must be importable from outside a checkout — no source tree needed.
    """
    check = subprocess.run(
        [str(installed_cli), "-c",
         "from mtm.docker import image_context_dir, image_context_files; "
         "import sys; d=image_context_dir(); fs=image_context_files(); "
         "assert (d/'Dockerfile').is_file(), d; "
         "assert 'Dockerfile' in fs and 'entrypoint.sh' in fs and 'toolchain.lock' in fs, fs; "
         "print('ok')"],
        capture_output=True, text=True, cwd=str(tmp_path),
    )
    assert check.returncode == 0, check.stderr
    assert "ok" in check.stdout

