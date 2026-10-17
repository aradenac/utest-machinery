"""Docker engine access: image build/check and container invocation.

Kept deliberately small: a thin wrapper over ``docker`` so the CLI can build and
check the generic image and run headless commands inside it. The product never
grants Docker broad workspace access; callers pass explicit mounts.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

import yaml

from .errors import Code, MtmError

IMAGE_NAME = "utest-machinery/mtm"
IMAGE_TAG = "1.0.0"


def image_ref(tag: str = IMAGE_TAG) -> str:
    return f"{IMAGE_NAME}:{tag}"


@dataclass
class DockerInfo:
    available: bool
    server_version: str = ""
    error: str = ""


def docker_available() -> DockerInfo:
    """Return whether a usable Docker daemon is reachable."""
    if shutil.which("docker") is None:
        return DockerInfo(False, error="'docker' not found on PATH")
    try:
        proc = subprocess.run(
            ["docker", "info", "--format", "{{.ServerVersion}}"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        return DockerInfo(False, error=str(exc))
    if proc.returncode != 0:
        return DockerInfo(False, error=proc.stderr.strip() or "docker info failed")
    return DockerInfo(True, server_version=proc.stdout.strip())


def image_exists(ref: str | None = None) -> bool:
    ref = ref or image_ref()
    proc = subprocess.run(
        ["docker", "image", "inspect", ref],
        capture_output=True,
        text=True,
        check=False,
    )
    return proc.returncode == 0


def image_digest(ref: str | None = None) -> str:
    ref = ref or image_ref()
    proc = subprocess.run(
        ["docker", "image", "inspect", ref, "--format", "{{index .RepoDigests 0}}"],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        return ""
    return proc.stdout.strip()


#: The image build context lives **inside the installed package**, so `mtm
#: install` works from a wheel as well as from a source checkout. It holds the
#: Dockerfile, entrypoint, licence/SBOM and toolchain lock.
PACKAGE_IMAGE_DIR = Path(__file__).resolve().parent / "image"


def image_context_dir() -> Path:
    """Directory holding the packaged image build context (``mtm/image``)."""
    return PACKAGE_IMAGE_DIR


def image_context_files() -> list[str]:
    """The packaged build-context files, as docker-context-relative paths."""
    return sorted(p.name for p in PACKAGE_IMAGE_DIR.iterdir() if p.is_file())


def build_image(tag: str = IMAGE_TAG) -> str:
    """Build the generic image from the packaged context. Raises MtmError.

    The context is assembled in a fresh temporary directory from the installed
    package (``mtm/image`` plus the ``mtm`` package source the Dockerfile
    copies), so the CLI can build the image autonomously — with no source
    checkout and no dependence on the current working directory. The Dockerfile
    ships in the package as ``mtm/image/Dockerfile`` and is copied into the
    assembled context as ``image/Dockerfile``.
    """
    import shutil
    import tempfile

    if not (PACKAGE_IMAGE_DIR / "Dockerfile").is_file():
        raise MtmError(
            code=Code.CONFIG_INVALID,
            message="Internal error: the packaged image build context is missing.",
            action="Reinstall uTest Machinery; the package data is incomplete.",
            stage="image-build",
        )
    package_root = PACKAGE_IMAGE_DIR.parent  # .../mtm
    with tempfile.TemporaryDirectory(prefix="mtm-image-") as tmp:
        ctx = Path(tmp)
        shutil.copytree(PACKAGE_IMAGE_DIR, ctx / "image")
        # The Dockerfile COPYs `mtm` into the image, so the package source must
        # be part of the context too (as `mtm/`, the package directory itself).
        shutil.copytree(package_root, ctx / "mtm")
        proc = subprocess.run(
            ["docker", "build", "-f", "image/Dockerfile", "-t", image_ref(tag), "."],
            cwd=ctx,
            capture_output=True,
            text=True,
            check=False,
        )
    if proc.returncode != 0:
        raise MtmError(
            code=Code.TOOL_FAILED,
            message="Docker image build failed.",
            action="Inspect the build output; fix the network access or Dockerfile.",
            stage="image-build",
            cause=proc.stderr.strip()[-2000:],
            tool_name="docker",
            retryable=True,
        )
    return image_ref(tag)


def base_image_digest(lock_file: Path | None = None) -> str:
    """Read the pinned base-image digest from the packaged ``toolchain.lock``."""
    lock_file = lock_file or (PACKAGE_IMAGE_DIR / "toolchain.lock")
    try:
        data = yaml.safe_load(lock_file.read_text(encoding="utf-8")) or {}
    except OSError:
        return ""
    return str(data.get("image", {}).get("base_digest", ""))
