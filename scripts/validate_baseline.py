#!/usr/bin/env python3
"""Fast, dependency-free documentation and C fixture sanity checks.

This validates the *specification baseline*, not the future uTest Machinery
implementation or full Mermaid rendering. Stage 01 will extend these checks.
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[1]
FAILURES: list[str] = []


def error(message: str) -> None:
    FAILURES.append(message)


def markdown() -> None:
    files = sorted(ROOT.rglob("*.md"))
    for file in files:
        if ".git" in file.parts:
            continue
        content = file.read_text(encoding="utf-8")
        fences = re.findall(r"^\s*\x60\x60\x60", content, re.MULTILINE)
        if len(fences) % 2:
            error(f"{file.relative_to(ROOT)}: unmatched Markdown code fence")
        if "\\n\\n" in content or "\\\x60" in content:
            error(f"{file.relative_to(ROOT)}: escaped Markdown delimiters detected")
        for target in re.findall(r"(?<!!)\]\(([^)]+)\)", content):
            url = target.strip().split("#", 1)[0]
            if not url or re.match(r"^(https?://|mailto:)", url):
                continue
            if url.startswith("<") and url.endswith(">"):
                url = url[1:-1]
            destination = (file.parent / unquote(url)).resolve()
            if not destination.exists():
                error(f"{file.relative_to(ROOT)}: broken link {target}")
        for block in re.findall(
            r"^\x60\x60\x60mermaid\s*\n(.*?)^\x60\x60\x60",
            content,
            flags=re.MULTILINE | re.DOTALL,
        ):
            first = next((s.strip() for s in block.splitlines() if s.strip()), "")
            if not re.match(
                r"^(flowchart|graph|sequenceDiagram|erDiagram|stateDiagram|classDiagram|gantt|journey|pie)\b",
                first,
            ):
                error(f"{file.relative_to(ROOT)}: unknown Mermaid diagram declaration")
            if len(re.findall(r"^\s*subgraph\b", block, re.MULTILINE)) != len(
                re.findall(r"^\s*end\s*$", block, re.MULTILINE)
            ):
                error(f"{file.relative_to(ROOT)}: unmatched Mermaid subgraph")
    print(f"Documentation: {len(files)} Markdown files inspected (structural checks only)")


def requirements() -> None:
    arch = (ROOT / "docs/architecture/architecture.md").read_text()
    lines = re.findall(r"^\| (SYS-\d{3}) \|", arch, re.MULTILINE)
    if lines != [f"SYS-{n:03d}" for n in range(1, 38)]:
        error("Architecture: expected unique ordered SYS-001..SYS-037")
    plan = (ROOT / "docs/plan/implementation.md").read_text()
    stages = re.findall(r"^\| \*\*(\d{2}) ", plan, re.MULTILINE)
    if stages != [f"{n:02d}" for n in range(1, 13)]:
        error("Implementation plan: expected stages 01..12")
    samples = sorted((ROOT / "examples").glob("*/sdd.md"))
    for sdd in samples:
        if not list(sdd.parent.glob("*.c")):
            error(f"{sdd.relative_to(ROOT)}: missing C source")
        if not re.search(r"\*\*[A-Z]{3,4}-\d{3}\*\*", sdd.read_text()):
            error(f"{sdd.relative_to(ROOT)}: no stable SDD requirement IDs")
    if len(samples) < 9:
        error(f"Fixture library: expected at least 9 C/SDD examples, got {len(samples)}")
    print(f"Requirements: {len(lines)} SYS, {len(stages)} stages, {len(samples)} C/SDD fixtures")


def fixtures() -> None:
    compiler = shutil.which("cc") or shutil.which("gcc") or shutil.which("clang")
    if compiler is None:
        error("No C compiler available to syntax-check example fixtures")
        return
    count = 0
    for file in sorted((ROOT / "examples").glob("*/*.c")):
        # Missing proprietary SDK header is intentional in this fixture.
        if file.parent.name == "vendor-adapter":
            continue
        result = subprocess.run(
            [compiler, "-std=c11", "-Wall", "-Wextra", "-Werror", "-pedantic",
             "-fsyntax-only", "-I", str(file.parent), str(file)],
            capture_output=True, text=True, check=False,
        )
        if result.returncode:
            error(f"{file.relative_to(ROOT)}: C syntax check failed: {result.stderr.strip()}")
        count += 1
    print(f"C fixtures: {count} compilation syntax checks (vendor adapter intentionally blocked)")


if __name__ == "__main__":
    markdown()
    requirements()
    fixtures()
    if FAILURES:
        for failure in FAILURES:
            print("ERROR:", failure, file=sys.stderr)
        sys.exit(1)
    print("Baseline checks PASS; full tool/LLM qualification is not yet implemented.")
