"""Native host feasibility classification: READY / ADAPTED / BLOCKED.

For every selected translation unit the engine decides, from **concrete
compilation evidence** (not from the LLM), whether the original C can be built
and executed on the host:

* ``READY``   — the original unit compiles as-is and has no unresolved externals.
* ``ADAPTED`` — the unit can be built only with generated private support: mocks
  for link-time externals, or a shim for a missing (e.g. vendor) header. Every
  assumption is recorded; the boundary of what was replaced is explicit.
* ``BLOCKED`` — the unit cannot be built for a reason we will not paper over
  (a genuine compile error, or no usable compiler), with concrete reasons.

This module never modifies a source file and never invents an interface: an
ADAPTED classification is an explicit, reviewable assumption that the later
generation/smoke stage must actually satisfy (``architecture.md`` executability
assessment). Host compilation is host-native evidence only — it is not a target
MCU guarantee.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .ccompile import CompileResult, HostCompiler
from .inventory import Inventory, Unit

READY = "READY"
ADAPTED = "ADAPTED"
BLOCKED = "BLOCKED"

_SEVERITY = {READY: 0, ADAPTED: 1, BLOCKED: 2}


@dataclass
class Feasibility:
    """The feasibility verdict for one unit."""

    unit_id: str
    path: str
    status: str
    reasons: list[str] = field(default_factory=list)
    assumptions: list[str] = field(default_factory=list)
    missing_headers: list[str] = field(default_factory=list)
    externals: list[str] = field(default_factory=list)
    diagnostics: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "unit_id": self.unit_id,
            "path": self.path,
            "status": self.status,
            "reasons": self.reasons,
            "assumptions": self.assumptions,
            "missing_headers": self.missing_headers,
            "externals": self.externals,
            "diagnostics": self.diagnostics,
        }


def _missing_header(diagnostics: str) -> str | None:
    """Extract the header name from a 'No such file or directory' diagnostic."""
    import re

    m = re.search(r"([^\s:]+\.h)[:\s]*No such file or directory", diagnostics)
    if not m:
        m = re.search(r"fatal error:\s*([^\s:]+)", diagnostics)
    return m.group(1) if m else None


def assess_unit(unit: Unit, unit_dir: Path, include_dirs: list[Path],
                compiler: HostCompiler) -> Feasibility:
    """Classify one unit from a real compile attempt of the original source."""
    if not compiler.available:
        return Feasibility(
            unit_id=unit.id, path=unit.path, status=BLOCKED,
            reasons=["no host C compiler available to assess this unit"],
        )

    source = None
    for candidate_dir in include_dirs:
        candidate = candidate_dir / Path(unit.path).name
        if candidate.is_file():
            source = candidate
            break
    if source is None:
        source = unit_dir / Path(unit.path).name

    includes = _unique_dirs([unit_dir, *include_dirs])
    result: CompileResult = compiler.syntax_check(source, includes)
    diagnostics = result.diagnostics

    if result.ok:
        if unit.externals:
            return Feasibility(
                unit_id=unit.id, path=unit.path, status=ADAPTED,
                reasons=[
                    "the original unit compiles on the host, but it calls "
                    f"{len(unit.externals)} external symbol(s) with no definition "
                    "in the selected scope",
                ],
                assumptions=[
                    "generate private mocks/stubs in the bench for: "
                    + ", ".join(unit.externals),
                ],
                externals=list(unit.externals),
                diagnostics=diagnostics,
            )
        return Feasibility(
            unit_id=unit.id, path=unit.path, status=READY,
            reasons=["the original unit compiles unchanged with no unresolved externals"],
            diagnostics=diagnostics,
        )

    header = _missing_header(diagnostics)
    missing = sorted(set(unit.missing_includes) | ({header} if header else set()))
    # A failure whose only cause is one or more missing headers is an ADAPTED
    # case: a private shim declaring the documented interface can satisfy it.
    other_errors = _non_missing_header_errors(diagnostics)
    if missing and not other_errors:
        return Feasibility(
            unit_id=unit.id, path=unit.path, status=ADAPTED,
            reasons=[
                "the original unit does not compile because header(s) are absent: "
                + ", ".join(missing),
            ],
            assumptions=[
                "generate a private compatible shim/mock header in the bench for: "
                + ", ".join(missing),
                "type and ABI of the shimmed interface must be taken from the SDD; "
                "the original source is not modified",
            ],
            missing_headers=missing,
            externals=list(unit.externals),
            diagnostics=diagnostics,
        )

    return Feasibility(
        unit_id=unit.id, path=unit.path, status=BLOCKED,
        reasons=["the original unit does not compile on the host, and the failure is "
                 "not explained by a missing header we could shim"],
        missing_headers=missing,
        externals=list(unit.externals),
        diagnostics=diagnostics,
    )


def assess_inventory(bench_root: Path, inventory: Inventory,
                     compiler: HostCompiler | None = None) -> list[Feasibility]:
    """Assess every unit in an inventory. Deterministic, no LLM."""
    compiler = compiler or HostCompiler()
    selected_files = [bench_root / s["path"] for s in inventory.selected
                      if not (bench_root / s["path"]).is_dir()]
    dirs: list[Path] = []
    for entry in inventory.selected:
        candidate = bench_root / entry["path"]
        dirs.append(candidate if candidate.is_dir() else candidate.parent)
    include_dirs = _unique_dirs(dirs + selected_files)

    verdicts: list[Feasibility] = []
    for unit in inventory.units:
        unit_dir = (bench_root / unit.path).parent
        verdicts.append(assess_unit(unit, unit_dir, include_dirs, compiler))
    return verdicts


def summarize(verdicts: list[Feasibility]) -> dict[str, int]:
    """Counts per status — the visible feasibility denominator (SYS-007)."""
    counts = {READY: 0, ADAPTED: 0, BLOCKED: 0}
    for verdict in verdicts:
        counts[verdict.status] = counts.get(verdict.status, 0) + 1
    return counts


def _unique_dirs(dirs: list[Path]) -> list[Path]:
    seen: set[str] = set()
    unique: list[Path] = []
    for path in dirs:
        resolved = str(path.resolve())
        if resolved not in seen and path.is_dir():
            seen.add(resolved)
            unique.append(path)
    return unique


def _non_missing_header_errors(diagnostics: str) -> list[str]:
    """Error lines that are *not* a missing-header diagnostic."""
    lines: list[str] = []
    for line in diagnostics.splitlines():
        low = line.lower()
        if "error" not in low:
            continue
        if "no such file or directory" in low:
            continue
        lines.append(line.strip())
    return lines
