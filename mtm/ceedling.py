"""Run a generated Ceedling project inside the product image.

The functional unit suite is a **standard Ceedling/CMock project**: the generator
writes ``project.yml``, the in-scope sources, the private shims, one dependency
header per mocked symbol and the Unity test files; Ceedling (a Ruby tool) then
generates the CMock mocks and the runner, compiles, links and executes.

Ceedling and its C toolchain live **in the product image**, never on the host: a
user needs no Ruby. This module only builds the ``docker run`` invocation and
parses the result. The bench is mounted because Ceedling writes its build output
under ``<bench>/.mtm/gen`` and reads the copied sources there; the *selected
inputs themselves* are never written to (SYS-004).

Two rules matter for the result contract (``workflow-failures.md``):

* a Unity summary line is the **only** proof the suite actually ran; its absence
  means the run did not produce test results, whatever the exit code;
* an abnormal termination (timeout, signal, or a non-zero exit with no summary)
  is a **tool failure**, never a C implementation defect.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

#: Ceedling writes and reads everything relative to this directory in the bench.
REVISION_DIRNAME = Path(".mtm") / "gen"

#: The bench is mounted read-write here; the project runs with this as CWD.
CONTAINER_BENCH = "/bench"

#: A writable HOME is needed so Ceedling can stage its vendored Unity/CMock.
CONTAINER_HOME = "/tmp"


@dataclass
class CeedlingRun:
    """Outcome of one Ceedling execution."""

    ok: bool
    returncode: int
    stdout: str = ""
    stderr: str = ""
    argv: list[str] = field(default_factory=list)
    timed_out: bool = False
    #: True when a parseable Unity summary was found: the suite really executed.
    ran: bool = False
    total: int = 0
    passed: int = 0
    failed: int = 0
    #: (test_function, reason) for each Unity assertion failure.
    failures: list[tuple[str, str]] = field(default_factory=list)

    @property
    def diagnostics(self) -> str:
        return (self.stderr or self.stdout).strip()


def _unity_summary(stdout: str) -> tuple[int, int, int] | None:
    """Parse Unity's ``N Tests M Failures K Ignored`` summary, or ``None``.

    Ceedling builds and runs **one executable per test file**, so a multi-unit
    scope produces several per-file summaries and no grand total. Sum them, so a
    multi-unit bench is not under-counted to its first file. Returns ``None`` when
    there is no summary at all — the only honest signal that nothing ran.
    """
    matches = list(re.finditer(r"(\d+)\s+Tests\s+(\d+)\s+Failures\s+(\d+)\s+Ignored", stdout))
    if not matches:
        return None
    total = sum(int(m.group(1)) for m in matches)
    failures = sum(int(m.group(2)) for m in matches)
    return total, total - failures, failures


def parse_failures(stdout: str) -> list[tuple[str, str]]:
    """Return ``(test_function, reason)`` for each Unity assertion failure.

    Unity prints ``<path>:<line>:<test>:FAIL: <reason>`` and the path itself
    contains ``:``, so the marker is split on the literal ``:FAIL`` token.
    """
    failures: list[tuple[str, str]] = []
    for line in stdout.splitlines():
        folded = line.replace(":", ":")
        if ":FAIL" not in folded or ":PASS" in folded:
            continue
        head, _, tail = folded.partition(":FAIL")
        marker = re.search(r"([A-Za-z_]\w*)\s*$", head)
        if not marker:
            continue
        failures.append((marker.group(1), tail.lstrip(": ").strip()))
    return failures


def run_ceedling(
    bench_root: Path,
    *,
    image_ref: str,
    user: str | None = None,
    timeout: int = 300,
) -> CeedlingRun:
    """Run ``ceedling test:all`` on the bench's generated project in the image."""
    argv = ["docker", "run", "--rm", "--user", user or _invoking_user() or "0:0",
            "--entrypoint", "ceedling",
            "-e", f"HOME={CONTAINER_HOME}",
            "-v", f"{Path(bench_root).resolve()}:{CONTAINER_BENCH}",
            "-w", f"{CONTAINER_BENCH}/{REVISION_DIRNAME.as_posix()}",
            image_ref, "test:all"]
    try:
        proc = subprocess.run(argv, capture_output=True, text=True,
                              timeout=timeout, check=False)
    except subprocess.TimeoutExpired as exc:
        return CeedlingRun(
            ok=False, returncode=124, timed_out=True, argv=argv,
            stdout=(exc.stdout or ""), stderr=f"Ceedling timed out after {timeout}s",
        )
    except OSError as exc:
        return CeedlingRun(ok=False, returncode=127, argv=argv, stderr=str(exc))

    out = proc.stdout or ""
    summary = _unity_summary(out)
    run = CeedlingRun(
        ok=proc.returncode == 0, returncode=proc.returncode, argv=argv,
        stdout=out, stderr=proc.stderr or "",
    )
    if summary is not None:
        run.ran = True
        run.total, run.passed, run.failed = summary
        run.failures = parse_failures(out)
    return run


def _invoking_user() -> str | None:
    import os

    try:
        if hasattr(os, "getuid"):
            return f"{os.getuid()}:{os.getgid()}"
    except OSError:  # pragma: no cover
        pass
    return None
