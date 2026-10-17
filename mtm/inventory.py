"""Deterministic inventory of selected inputs: units, requirements, snapshot.

Builds the *facts* half of the evidence model from the bench scope
(``docs/architecture/data-contracts.md``: ``InputSnapshot``, ``Unit``,
``Requirement``). Everything here is derived by code — no LLM, no heuristics
that could invent a requirement or a symbol. The LLM stage consumes this
inventory; it never authors it.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .cscan import external_calls, scan_c
from .errors import Code, MtmError

#: Stable requirement id shape used by the fixture SDDs, e.g. ``SAT-001``.
_REQ_PREFIXES = ("SDD", "SPEC", "REQ")


@dataclass
class Requirement:
    """An SDD-anchored requirement extracted verbatim from a document."""

    id: str
    statement: str
    source_path: str
    source_line: int
    document: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "statement": self.statement,
            "source_path": self.source_path,
            "source_line": self.source_line,
            "document": self.document,
        }


@dataclass
class Unit:
    """A selected translation unit and its host-compilation facts."""

    id: str
    path: str
    sha256: str
    functions: list[dict[str, Any]] = field(default_factory=list)
    includes_system: list[str] = field(default_factory=list)
    includes_local: list[str] = field(default_factory=list)
    externals: list[str] = field(default_factory=list)
    missing_includes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "path": self.path,
            "sha256": self.sha256,
            "functions": self.functions,
            "includes_system": self.includes_system,
            "includes_local": self.includes_local,
            "externals": self.externals,
            "missing_includes": self.missing_includes,
        }


@dataclass
class Inventory:
    """The deterministic input inventory for a generation run."""

    snapshot_hash: str
    selected: list[dict[str, Any]]
    units: list[Unit] = field(default_factory=list)
    requirements: list[Requirement] = field(default_factory=list)
    spec_documents: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "snapshot_hash": self.snapshot_hash,
            "selected": self.selected,
            "units": [u.to_dict() for u in self.units],
            "requirements": [r.to_dict() for r in self.requirements],
            "spec_documents": self.spec_documents,
        }


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def resolve_scope_paths(bench_root: Path, selected: list[dict[str, Any]]) -> list[Path]:
    """Resolve each selected scope entry to concrete files under *bench_root*.

    A selected entry may be a file or a directory; a directory contributes its
    files recursively. Paths are resolved relative to the bench root unless
    absolute. No path outside the selected entries is ever inspected (SYS-005).
    """
    resolved: list[Path] = []
    for entry in selected:
        raw = entry.get("path", "")
        candidate = Path(raw)
        if not candidate.is_absolute():
            candidate = (bench_root / raw)
        candidate = candidate.resolve()
        if not candidate.exists():
            raise MtmError(
                code=Code.INPUT_MISSING,
                message=f"A selected path no longer exists: {raw}",
                action="Correct the scope with 'mtm scope remove' / 'mtm scope add', then retry.",
                stage="generate",
                input_reference=raw,
            )
        if candidate.is_dir():
            resolved.extend(sorted(p for p in candidate.rglob("*") if p.is_file()))
        else:
            resolved.append(candidate)
    # De-duplicate while preserving deterministic order.
    seen: set[str] = set()
    unique: list[Path] = []
    for path in resolved:
        key = str(path)
        if key not in seen:
            seen.add(key)
            unique.append(path)
    return unique


def snapshot_hash(paths: list[Path]) -> str:
    """Content-addressed hash of the selected input bytes.

    Hashes ``relpath\\0sha256\\n`` for each file in a deterministic order, so the
    snapshot identity changes if and only if a selected input's bytes or path set
    change.
    """
    h = hashlib.sha256()
    for path in sorted(paths, key=lambda p: str(p)):
        h.update(str(path).encode("utf-8"))
        h.update(b"\0")
        h.update(_sha256(path).encode("ascii"))
        h.update(b"\n")
    return h.hexdigest()


def extract_requirements(doc_path: Path, doc_text: str | None = None) -> list[Requirement]:
    """Extract ``**ID-nnn**: statement`` requirements from an SDD document.

    A conservative line-oriented scan: the id must be bold, upper-case, of the
    ``PREFIX-NNN`` shape. The statement keeps the document's own wording; nothing
    is paraphrased. Lines without a matching bold id are ignored.
    """
    text = doc_text if doc_text is not None else doc_path.read_text(encoding="utf-8", errors="replace")
    import re

    pattern = re.compile(r"^\s*[-*]?\s*\*\*([A-Z]{2,6}-\d{1,4})\*\*\s*:?\s*(.*)$")
    requirements: list[Requirement] = []
    for line_no, line in enumerate(text.splitlines(), start=1):
        m = pattern.match(line)
        if not m:
            continue
        statement = m.group(2).strip()
        if not statement:
            continue
        requirements.append(Requirement(
            id=m.group(1),
            statement=statement,
            source_path=str(doc_path),
            source_line=line_no,
            document=doc_path.name,
        ))
    return requirements


def build_inventory(bench_root: Path, selected: list[dict[str, Any]]) -> Inventory:
    """Assemble the deterministic inventory from the selected scope.

    ``selected`` entries are the scope dicts stored in ``project.yaml`` (each
    carrying ``path`` and ``kind``).
    """
    paths = resolve_scope_paths(bench_root, selected)
    if not paths:
        raise MtmError(
            code=Code.INPUT_MISSING,
            message="No inputs are selected, so there is nothing to generate from.",
            action="Select the source/SDD inputs first, e.g. 'mtm scope add <path> --kind source'.",
            stage="generate",
        )

    c_units: list[Unit] = []
    requirements: list[Requirement] = []
    spec_documents: list[str] = []

    # Map a local include name -> the file providing it, among selected files.
    by_name: dict[str, Path] = {p.name: p for p in paths}

    for path in paths:
        if path.suffix.lower() == ".c":
            unit = scan_c(path)
            unit_id = f"unit-{_sha256(path)[:12]}"
            missing = [
                inc.name
                for inc in unit.includes
                if not inc.system and inc.name not in by_name
            ]
            c_units.append(Unit(
                id=unit_id,
                path=_relative(path, bench_root),
                sha256=_sha256(path),
                functions=[
                    {"name": f.name, "signature": f.signature, "line": f.line}
                    for f in unit.functions
                ],
                includes_system=sorted({i.name for i in unit.includes if i.system}),
                includes_local=sorted({i.name for i in unit.includes if not i.system}),
                externals=external_calls(unit),
                missing_includes=sorted(missing),
            ))
        elif path.suffix.lower() in (".md", ".txt", ".yaml", ".yml"):
            found = extract_requirements(path)
            if found:
                requirements.extend(found)
                spec_documents.append(_relative(path, bench_root))

    return Inventory(
        snapshot_hash=snapshot_hash(paths),
        selected=[{"path": e.get("path"), "kind": e.get("kind")} for e in selected],
        units=c_units,
        requirements=requirements,
        spec_documents=sorted(spec_documents),
    )


def _relative(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
    except ValueError:
        return str(path)
