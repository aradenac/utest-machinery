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


def build_image(context: Path, tag: str = IMAGE_TAG) -> str:
    """Build the generic image. Raises :class:`MtmError` on failure.

    The image build context is the **source tree** (``image/Dockerfile`` plus the
    ``mtm`` sources it copies), not the installed package: a wheel install ships
    the control CLI and its data, not a Docker context. Point ``--context`` at a
    source checkout to build.
    """
    if not (context / "image" / "Dockerfile").is_file():
        raise MtmError(
            code=Code.CONFIG_INVALID,
            message=f"No image/Dockerfile found under the build context {context}.",
            action=("Building the generic image requires the uTest Machinery source tree. "
                    "Run 'mtm install --context <source-dir>' (or run mtm from the checkout). "
                    "An installed wheel provides the control commands, not a Docker context."),
            stage="image-build",
            input_reference=str(context),
        )
    proc = subprocess.run(
        ["docker", "build", "-f", "image/Dockerfile", "-t", image_ref(tag), "."],
        cwd=context,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        raise MtmError(
            code=Code.TOOL_FAILED,
            message="Docker image build failed.",
            action="Inspect the build output; fix the Dockerfile or network access.",
            stage="image-build",
            cause=proc.stderr.strip()[-2000:],
            tool_name="docker",
            retryable=True,
        )
    return image_ref(tag)


def base_image_digest(lock_file: Path) -> str:
    """Read the pinned base-image digest from ``image/toolchain.lock``."""
    data = yaml.safe_load(lock_file.read_text(encoding="utf-8")) or {}
    return str(data.get("image", {}).get("base_digest", ""))
