"""Bench layout, project configuration and the operation status journal.

All project-specific persistent state lives under one bench root that is
host-mounted (``docs/architecture/architecture.md``). Nothing significant is
written inside the container image. Secrets are never stored here.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from . import MILESTONE, SCHEMA_VERSION
from .errors import Code, MtmError

PROJECT_FILE = "project.yaml"
CONFIG_DIR = ".mtm"
STATE_DIR = "state"
LAST_OPERATION = "last-operation.json"

#: Subdirectories created by ``init`` inside the bench root.
BENCH_SUBDIRS = ("workflows", "requirements", "tests", "mocks", "fuzz", "evidence", "reports")


def find_bench_root(start: Path | None = None) -> Path | None:
    """Walk upward from *start* (default: the current directory) for a bench."""
    current = (start or Path.cwd()).resolve()
    for candidate in (current, *current.parents):
        if (candidate / PROJECT_FILE).is_file():
            return candidate
    return None


def require_bench_root(start: Path | None = None) -> Path:
    root = find_bench_root(start or Path.cwd())
    if root is None:
        raise MtmError(
            code=Code.BENCH_MISSING,
            message="No test bench found in this directory or any parent.",
            action="Run 'mtm init' to create a bench, or change to a directory inside one.",
            stage="resolve-bench",
        )
    return root


@dataclass
class ProjectConfig:
    """Parsed ``project.yaml``. Unknown fields are preserved verbatim."""

    bench_id: str
    schema_version: int = SCHEMA_VERSION
    released_milestone: str = MILESTONE
    source_roots: list[str] = field(default_factory=list)
    spec_roots: list[str] = field(default_factory=list)
    selectors: dict[str, Any] = field(default_factory=dict)
    scopes: list[dict[str, Any]] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def load(cls, project_file: Path) -> "ProjectConfig":
        try:
            data = yaml.safe_load(project_file.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            raise MtmError(
                code=Code.CONFIG_INVALID,
                message=f"Invalid YAML in {project_file.name}: {exc}",
                action="Fix the configuration file syntax and retry.",
                stage="load-config",
                input_reference=str(project_file),
            ) from exc
        if not isinstance(data, dict):
            raise MtmError(
                code=Code.CONFIG_INVALID,
                message=f"{project_file.name} must contain a mapping.",
                action="Replace the file with a valid project configuration.",
                stage="load-config",
                input_reference=str(project_file),
            )
        version = data.get("schema_version")
        if version != SCHEMA_VERSION:
            raise MtmError(
                code=Code.CONFIG_INVALID,
                message=(
                    f"Unsupported schema_version {version!r} in {project_file.name}; "
                    f"this build understands {SCHEMA_VERSION}."
                ),
                action="Migrate the project file, or use the matching mtm version.",
                stage="load-config",
                input_reference=str(project_file),
            )
        _validate_against_schema(data, project_file)
        return cls(
            bench_id=str(data.get("bench_id", "")),
            schema_version=version,
            released_milestone=str(data.get("released_milestone", MILESTONE)),
            source_roots=list(data.get("source_roots") or []),
            spec_roots=list(data.get("spec_roots") or []),
            selectors=dict(data.get("selectors") or {}),
            scopes=list(data.get("scopes") or []),
            raw=data,
        )

    def to_dict(self) -> dict[str, Any]:
        data = dict(self.raw)
        data.update(
            schema_version=self.schema_version,
            bench_id=self.bench_id,
            released_milestone=self.released_milestone,
            source_roots=self.source_roots,
            spec_roots=self.spec_roots,
            selectors=self.selectors,
            scopes=self.scopes,
        )
        return data


_SCHEMA_DIR = Path(__file__).resolve().parent / "schemas"


def _validate_against_schema(data: dict[str, Any], project_file: Path) -> None:
    """Validate a parsed config against the versioned ProjectConfig schema.

    Unknown fields are allowed (the schema sets additionalProperties true), so a
    newer config is not rejected for additive changes; structural errors are.
    The schema ships as package data, so a wheel install validates too; if it is
    somehow missing, this fails loudly rather than silently skipping validation.
    """
    schema_file = _SCHEMA_DIR / "project.schema.json"
    if not schema_file.is_file():
        raise MtmError(
            code=Code.EVIDENCE_INVALID,
            message="Internal error: the ProjectConfig schema is not installed.",
            action="Reinstall uTest Machinery; the package data is incomplete.",
            stage="load-config",
        )
    try:
        import json

        import jsonschema

        schema = json.loads(schema_file.read_text(encoding="utf-8"))
        jsonschema.validate(data, schema)
    except Exception as exc:  # jsonschema.ValidationError or a parse error
        raise MtmError(
            code=Code.CONFIG_INVALID,
            message=f"{project_file.name} does not satisfy the ProjectConfig schema: {exc}",
            action="Correct the configuration file, or re-run 'mtm init'.",
            stage="load-config",
            input_reference=str(project_file),
        ) from exc


def write_project(root: Path, config: ProjectConfig) -> Path:
    """Atomically write ``project.yaml``."""
    target = root / PROJECT_FILE
    tmp = target.with_suffix(".yaml.tmp")
    tmp.write_text(
        yaml.safe_dump(config.to_dict(), sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    os.replace(tmp, target)
    return target


def state_dir(root: Path) -> Path:
    path = root / CONFIG_DIR / STATE_DIR
    path.mkdir(parents=True, exist_ok=True)
    return path


def logs_dir(root: Path) -> Path:
    path = state_dir(root) / "logs"
    path.mkdir(parents=True, exist_ok=True)
    return path


def write_last_operation(root: Path, record: dict[str, Any]) -> Path:
    """Persist the most recent operation status atomically."""
    target = state_dir(root) / LAST_OPERATION
    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(record, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, target)
    return target


def read_last_operation(root: Path) -> dict[str, Any] | None:
    target = root / CONFIG_DIR / STATE_DIR / LAST_OPERATION
    if not target.is_file():
        return None
    try:
        return json.loads(target.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


def new_bench_id(root: Path) -> str:
    """Derive a stable, unique bench identifier from the absolute path."""
    digest = hashlib.sha256(str(root.resolve()).encode("utf-8")).hexdigest()[:12]
    return f"bench-{digest}"


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()
