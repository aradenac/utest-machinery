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

#: ``docker run`` exit statuses that are **not** a test result, from the Docker
#: documentation. The reserved statuses are 125 (the daemon could not run the
#: container), 126 (the requested command could not be invoked) and 127 (the
#: requested command could not be found); a container killed by a Unix signal is
#: reported as ``128 + signal`` (SIGKILL 9 → 137, SIGTERM 15 → 143, SIGABRT 6 →
#: 134, …). Any of these can be observed *after* the runner already printed a
#: parseable Unity failure summary (a kill mid-run), so they must be classified as
#: a tool fault even when an assertion finding is also present — a genuine
#: Ceedling assertion exit is a small positive status (Unity/Ceedling return 1).
DOCKER_RESERVED_EXITS = frozenset({125, 126, 127})

#: The highest Unix signal number Docker encodes as ``128 + signal``.
DOCKER_MAX_SIGNAL = 64

#: Names for the signals a Docker ``128 + signal`` status most commonly encodes.
_SIGNAL_NAMES = {2: "SIGINT", 6: "SIGABRT", 9: "SIGKILL", 11: "SIGSEGV", 15: "SIGTERM"}


def _signal_number(returncode: int) -> int | None:
    """The signal Docker's ``128 + signal`` status encodes, or ``None``."""
    if 128 < returncode <= 128 + DOCKER_MAX_SIGNAL:
        return returncode - 128
    return None


def docker_fault_reason(returncode: int) -> str | None:
    """Why a ``docker run`` status is a tool fault rather than a test result.

    ``None`` when *returncode* is an ordinary Ceedling exit (e.g. 0 or 1), so the
    caller classifies by the Unity evidence instead. Never reclassifies a normal
    assertion exit: only Docker's own reserved statuses and signal deaths match.
    """
    if returncode in DOCKER_RESERVED_EXITS:
        return {
            125: f"the Docker daemon could not start the container ({returncode})",
            126: f"the requested command could not be invoked in the container ({returncode})",
            127: f"the requested command was not found in the container ({returncode})",
        }[returncode]
    signal = _signal_number(returncode)
    if signal is not None:
        name = _SIGNAL_NAMES.get(signal, f"signal {signal}")
        return f"the container was killed ({returncode} = {name}) during the test run"
    return None


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
    #: Tests Unity enumerated but *skipped* (``K Ignored``). An ignored test did
    #: not execute its body, so it must not count as executed coverage.
    ignored: int = 0
    #: (test_function, reason) for each Unity assertion failure.
    failures: list[tuple[str, str]] = field(default_factory=list)

    @property
    def executed(self) -> int:
        """Tests whose body actually ran (enumerated minus ignored)."""
        return max(self.total - self.ignored, 0)

    @property
    def diagnostics(self) -> str:
        return (self.stderr or self.stdout).strip()


def _unity_summary(stdout: str) -> tuple[int, int, int, int] | None:
    """Parse Unity's ``N Tests M Failures K Ignored`` summary, or ``None``.

    Ceedling builds and runs **one executable per test file**, so a multi-unit
    scope produces several per-file summaries and no grand total. Sum them, so a
    multi-unit bench is not under-counted to its first file. Returns ``None`` when
    there is no summary at all — the only honest signal that nothing ran.

    The ignored count is retained: an ignored test is enumerated but its body does
    not run, so it must not be mistaken for executed coverage — nor counted as a
    *pass*. ``passed`` is therefore ``total - failures - ignored``, so an
    all-ignored run honestly reports zero passes.
    """
    matches = list(re.finditer(r"(\d+)\s+Tests\s+(\d+)\s+Failures\s+(\d+)\s+Ignored", stdout))
    if not matches:
        return None
    total = sum(int(m.group(1)) for m in matches)
    failures = sum(int(m.group(2)) for m in matches)
    ignored = sum(int(m.group(3)) for m in matches)
    return total, max(total - failures - ignored, 0), failures, ignored


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
    """Run ``ceedling test:all`` on the bench's generated project in the image.

    The bench is mounted **read-only** and only the generated revision directory
    is writable: Ceedling reads the copied sources under ``.mtm/gen`` and writes
    its build output there, but the selected inputs elsewhere in the bench stay
    unreachable for writing (SYS-004). This mirrors the Dagu path's separation
    (``engine.py``: ``/bench:ro`` plus a writable ``/bench/.mtm``).
    """
    bench = Path(bench_root).resolve()
    revision = REVISION_DIRNAME.as_posix()
    argv = ["docker", "run", "--rm", "--user", user or _invoking_user() or "0:0",
            "--entrypoint", "ceedling",
            "-e", f"HOME={CONTAINER_HOME}",
            "-v", f"{bench}:{CONTAINER_BENCH}:ro",
            "-v", f"{bench / REVISION_DIRNAME}:{CONTAINER_BENCH}/{revision}",
            "-w", f"{CONTAINER_BENCH}/{revision}",
            image_ref, "test:all"]
    try:
        proc = subprocess.run(argv, capture_output=True, text=True,
                              timeout=timeout, check=False)
    except subprocess.TimeoutExpired as exc:
        # ``TimeoutExpired.stdout`` is ``bytes`` even when the call used
        # ``text=True`` (a documented Python quirk). Decode it so the caller can
        # carry it into the JSON result; a raw bytes payload would make
        # ``json.dumps`` raise instead of publishing a structured TOOL_FAILURE.
        return CeedlingRun(
            ok=False, returncode=124, timed_out=True, argv=argv,
            stdout=_as_text(exc.stdout),
            stderr=f"Ceedling timed out after {timeout}s",
        )
    except OSError as exc:
        return CeedlingRun(ok=False, returncode=127, argv=argv, stderr=str(exc))

    out = _as_text(proc.stdout)
    summary = _unity_summary(out)
    run = CeedlingRun(
        ok=proc.returncode == 0, returncode=proc.returncode, argv=argv,
        stdout=out, stderr=_as_text(proc.stderr),
    )
    if summary is not None:
        run.ran = True
        run.total, run.passed, run.failed, run.ignored = summary
        run.failures = parse_failures(out)
    return run


def _as_text(value: str | bytes | None) -> str:
    """Coerce possibly-bytes subprocess output to ``str`` (lossy, never raising).

    ``subprocess`` returns ``bytes`` from ``TimeoutExpired`` even under
    ``text=True``; every string that reaches the JSON result must already be
    ``str`` or serialization fails.
    """
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def _invoking_user() -> str | None:
    import os

    try:
        if hasattr(os, "getuid"):
            return f"{os.getuid()}:{os.getgid()}"
    except OSError:  # pragma: no cover
        pass
    return None
