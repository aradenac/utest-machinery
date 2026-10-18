"""The ``verify`` operation: replay a frozen bench offline, across profiles.

``mtm verify`` is the **no-LLM** half of the workflow (SYS-015). It does not
generate, plan or call a model: it re-runs the *already generated* Ceedling
project against the **same selected input bytes** that produced it, records
immutable per-profile evidence and reports honest statuses.

Three contracts this module exists to honour (``docs/plan/implementation.md``
PR 3 DoD, ``docs/architecture/workflow-failures.md``):

1. **Frozen replay / STALE_INPUT.** The stored plan carries the snapshot hash of
   the inputs it was planned against. Verify recomputes that hash from the
   current selected bytes and refuses to run when it differs — a changed
   source/SDD/scope must yield ``SNAPSHOT_MISMATCH`` (exit 40), never a
   misleading replay. A changed *execution configuration* (profiles/enabled set)
   invalidates evidence for the affected profiles but does not change test
   content.
2. **Profile truthfulness.** Each profile reports ``PASSED``/``FAILED`` (with its
   native report), ``UNSUPPORTED`` (tool absent, named) or ``TOOL_FAILED`` (the
   run produced no usable report). A tool fault is never a functional pass or a
   functional failure, and no profile claims target-MCU behaviour.
3. **No edits.** Selected sources, the generated tests and the approved oracles
   are never modified; only new run records are appended under
   ``<bench>/.mtm/state/verify``.

The functional profile reuses the PR 2 Ceedling runner verbatim. The other
profiles re-run the *same unchanged* sources/tests through a per-profile
``project.yml`` in a private subdirectory of the frozen revision, so an
instrumented build cannot reuse another profile's objects and the base revision
is never mutated.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .ceedling import (
    CONTAINER_BENCH,
    REVISION_DIRNAME,
    CeedlingRun,
    docker_fault_reason,
    parse_failures,
    run_ceedling,
)
from .errors import Code, MtmError
from .paths import read_json, sha256_file, write_json_atomic
from .profiles import (
    FAILED,
    NOT_APPLICABLE,
    PASSED,
    PROFILE_BUILD_ROOT,
    PROFILES,
    TOOL_FAILED,
    UNSUPPORTED,
    Profile,
)

#: Where the verify operation writes its run records (host-side, reviewable).
VERIFY_STATE = Path(".mtm") / "state" / "verify"
#: The frozen plan the run is replayed from.
PLAN_FILE = Path(".mtm") / "state" / "generate" / "plan.json"
INVENTORY_FILE = Path(".mtm") / "state" / "generate" / "inventory.json"

#: Subdirectory of the frozen revision holding per-profile ``project.yml`` files.
PROFILE_DIRNAME = "profiles"

#: LLVM 18 tools the coverage profile needs, under their versioned PATH names
#: (Debian's ``clang-18``/``llvm-18`` packages install these suffixes so they can
#: coexist with an unversioned toolchain).
_LLVM_PROFDATA = "llvm-profdata-18"
_LLVM_COV = "llvm-cov-18"


@dataclass
class ProfileResult:
    """The outcome of one profile. ``evidence`` is JSON-serialisable."""

    id: str
    status: str
    reason: str = ""
    #: Native metric summary (host-scoped), e.g. Unity totals or coverage.
    metrics: dict[str, Any] = field(default_factory=dict)
    #: Raw artefact paths (bench-relative), preserved for audit.
    artefacts: list[str] = field(default_factory=list)
    command: str = ""
    exit_code: int | None = None
    tool_versions: dict[str, str] = field(default_factory=dict)
    diagnostics: str = ""
    limits: str = ""
    #: Raw native output, written into the *run's* evidence directory (never a
    #: shared path) so a campaign stays immutable and reviewable.
    raw_stdout: str = ""
    raw_stderr: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "status": self.status,
            "reason": self.reason,
            "metrics": self.metrics,
            "artefacts": self.artefacts,
            "command": self.command,
            "exit_code": self.exit_code,
            "tool_versions": self.tool_versions,
            "diagnostics": self.diagnostics[-4000:],
            "limits": self.limits,
        }


@dataclass
class VerifyResult:
    status: str
    exit_code: int
    run_id: str
    profiles: list[ProfileResult] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)
    snapshot_hash: str = ""
    findings: list[dict[str, Any]] = field(default_factory=list)
    evidence_dir: str = ""
    toolchain: dict[str, Any] = field(default_factory=dict)


# --- snapshot / staleness ---------------------------------------------------


def _inventory_snapshot(bench_root: Path) -> str:
    data = read_json(bench_root / INVENTORY_FILE)
    return str((data or {}).get("snapshot_hash", "")) if isinstance(data, dict) else ""


def _current_snapshot(bench_root: Path, selected: list[dict[str, Any]]) -> str:
    from .inventory import snapshot_hash
    from .inventory import resolve_scope_paths

    return snapshot_hash(resolve_scope_paths(bench_root, selected))


def check_frozen(bench_root: Path, selected: list[dict[str, Any]],
                 *, force: bool = False) -> str:
    """Return the frozen snapshot hash, or raise ``SNAPSHOT_MISMATCH``.

    A frozen suite may only be replayed against the exact bytes it was generated
    from. If the selected inputs changed (content, or the selected path set), the
    stored plan's snapshot no longer matches the current one and verify refuses —
    the user must regenerate for the new snapshot (``architecture.md`` SYS-033)
    or restore the recorded revision. A missing plan is a plain input error.
    """
    plan = read_json(bench_root / PLAN_FILE)
    if not isinstance(plan, dict):
        raise MtmError(
            code=Code.INPUT_MISSING,
            message="No frozen plan exists for this bench, so there is nothing to verify.",
            action="Run 'mtm generate' to produce a plan and suite, then 'mtm verify'.",
            stage="verify",
        )
    frozen = str(plan.get("snapshot_hash", ""))
    current = _current_snapshot(bench_root, selected)
    if not force and frozen and current and frozen != current:
        raise MtmError(
            code=Code.SNAPSHOT_MISMATCH,
            message=("The selected inputs changed since the plan was frozen, so the "
                     "frozen suite no longer matches them."),
            action="Re-run 'mtm generate' for the current inputs, or restore the "
                   "recorded input revision; a frozen campaign is never replayed "
                   "against changed inputs.",
            stage="verify",
            input_reference=f"snapshot {frozen[:12]} -> {current[:12]}",
        )
    return frozen or current


# --- profile execution ------------------------------------------------------


def _revision(bench_root: Path) -> Path:
    return bench_root / REVISION_DIRNAME


def _profile_project_yml(base_yml: str, profile: Profile) -> str:
    """Extend the frozen ``project.yml`` for one profile, run from a subdirectory.

    The frozen project file is the *single* source of the suite's structure; this
    variant re-points each relative ``:paths:`` entry one level up (``../``) so it
    still resolves to the same files when Ceedling runs from
    ``<revision>/profiles/<id>/``, and gives the profile its own ``:build_root:``
    (so an instrumented build never reuses another profile's objects). Sources and
    tests are never touched — the paths only *reach* the same frozen files.

    Ceedling resolves ``:paths:`` relative to its working directory *and* expands
    the ``**`` globs, so a bare ``src/**`` from the subdirectory would find nothing;
    the ``../../``-prefixed entries are what keep every profile compiling the exact
    same source and test set as the base run.
    """
    text = re.sub(r"(?m)^(\s*:build_root:).*$",
                  f"\\1 {PROFILE_BUILD_ROOT.get(profile.id, f'build-{profile.id}')}",
                  base_yml)
    # Re-point only the relative *path* entries under :paths: up two levels (the
    # profile work directory is ``<revision>/profiles/<id>/``), so they still reach
    # the same frozen files. A path entry is a list item that is not a Ceedling
    # symbol (`:ignore`, `:return_thru_ptr`, …): those are left untouched, or the
    # `:cmock:` plugin list would be corrupted. `:build_root:` stays *local* to the
    # profile directory, which keeps each profile's objects and report artefacts
    # under its own tree (`find .` then finds them).
    text = re.sub(r"(?m)^(\s*- )(?!\.\./|:)(\S+)$", r"\1../../\2", text)
    text = text.rstrip("\n")
    if profile.compiler:
        text += (
            "\n:tools:\n"
            "  :test_compiler:\n"
            f"    :executable: {profile.compiler}\n"
            "  :test_linker:\n"
            f"    :executable: {profile.compiler}\n"
        )
    # Flags are emitted only for a *non-empty* list: Ceedling rejects an empty
    # ``:flags: :test: :link: :*`` as "a nilclass", so a profile with no link
    # flags (e.g. the portability/`-O2` profile) must omit the whole ``:flags:``
    # block rather than declare it empty.
    if profile.compile_flags or profile.link_flags:
        text += ":flags:\n  :test:\n"
        if profile.compile_flags:
            text += "    :compile:\n      :*:\n"
            for flag in profile.compile_flags:
                text += f"        - {flag}\n"
        if profile.link_flags:
            text += "    :link:\n      :*:\n"
            for flag in profile.link_flags:
                text += f"        - {flag}\n"
    return text + "\n"


def _write_profile_projects(bench_root: Path) -> dict[str, Path]:
    """Write a per-profile ``project.yml`` under the frozen revision.

    Written into the bench's own frozen directory (not the inputs), and refreshed
    on every verify run, so a profile config can never go stale silently. Returns
    ``{profile_id: workdir}`` (the directory Ceedling must run in).
    """
    revision = _revision(bench_root)
    base = (revision / "project.yml").read_text(encoding="utf-8")
    workdirs: dict[str, Path] = {}
    for profile in PROFILES:
        workdir = revision / PROFILE_DIRNAME / profile.id
        workdir.mkdir(parents=True, exist_ok=True)
        (workdir / "project.yml").write_text(
            _profile_project_yml(base, profile), encoding="utf-8")
        workdirs[profile.id] = workdir
    return workdirs


def _docker_prefix(bench_root: Path, image_ref: str, *, user: str | None = None) -> list[str]:
    """``docker run`` options for a bench mount (no image, no command yet).

    The bench is mounted read-only and only the frozen revision directory is
    writable, exactly as the PR 2 functional runner does, so no profile can write
    to the selected inputs (SYS-004) — *argv* only ever builds and runs *inside*
    the revision directory.
    """
    import os

    uid = user or (f"{os.getuid()}:{os.getgid()}" if hasattr(os, "getuid") else "0:0")
    return ["docker", "run", "--rm", "--user", uid, "-e", "HOME=/tmp",
            "-v", f"{bench_root.resolve()}:{CONTAINER_BENCH}:ro",
            "-v", f"{(bench_root / REVISION_DIRNAME).resolve()}:{CONTAINER_BENCH}/{REVISION_DIRNAME.as_posix()}"]


def _run_in_image(bench_root: Path, image_ref: str, workdir: Path,
                  command: list[str], *, env: dict[str, str] | None = None,
                  timeout: int = 600) -> subprocess.CompletedProcess:
    """Run *command* inside the image with the bench mounted, CWD = *workdir*.

    ``docker run`` requires every option *before* the image reference, so the
    command (image, then argv) is assembled last: getting this order wrong makes
    Docker treat the command as an image name. The bench is mounted read-only
    except the frozen revision, so no profile can write to the selected inputs
    (SYS-004).
    """
    rel = workdir.relative_to(bench_root).as_posix()
    docker = _docker_prefix(bench_root, image_ref)
    for key, value in (env or {}).items():
        docker += ["-e", f"{key}={value}"]
    docker += ["-w", f"{CONTAINER_BENCH}/{rel}", "--entrypoint", command[0],
               image_ref, *command[1:]]
    try:
        return subprocess.run(docker, capture_output=True, text=True,
                              timeout=timeout, check=False)
    except subprocess.TimeoutExpired as exc:
        return subprocess.CompletedProcess(
            docker, 124, _as_text(exc.stdout), f"timed out after {timeout}s")
    except OSError as exc:
        return subprocess.CompletedProcess(docker, 127, "", str(exc))


def _as_text(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value or ""


def _tool_version(bench_root: Path, image_ref: str, tool: str) -> str:
    proc = _run_in_image(bench_root, image_ref, _revision(bench_root),
                         ["bash", "-lc", f"{tool} --version 2>&1 | head -1"], timeout=120)
    return (proc.stdout or "").strip().splitlines()[0] if proc.stdout else ""


def _have_tool(bench_root: Path, image_ref: str, profile: Profile) -> str | None:
    """Return the first required tool that is missing, or ``None`` when present.

    A missing tool makes the profile `UNSUPPORTED` with the tool named — never an
    invented pass — so a reduced image is reported honestly instead of silently
    dropping a profile.
    """
    for tool in profile.requires:
        proc = _run_in_image(bench_root, image_ref, _revision(bench_root),
                             ["bash", "-lc", f"command -v {tool}"], timeout=120)
        if proc.returncode != 0:
            return tool
    return None


def _unity_summary(stdout: str) -> tuple[int, int, int] | None:
    matches = list(re.finditer(r"(\d+)\s+Tests\s+(\d+)\s+Failures\s+(\d+)\s+Ignored", stdout))
    if not matches:
        return None
    return (sum(int(m.group(1)) for m in matches),
            sum(int(m.group(2)) for m in matches),
            sum(int(m.group(3)) for m in matches))


def _classify_build_run(proc: subprocess.CompletedProcess,
                        profile: Profile, build_root: str) -> ProfileResult:
    """Classify a profile run from its exit + a parseable Unity summary.

    A summary is the only proof the tests actually executed: without one the run
    is a tool fault, whatever the exit code. A non-zero exit *with* a summary is a
    genuine test failure (a finding), unless Docker's own statuses say the
    container died.
    """
    out = _as_text(proc.stdout)
    err = _as_text(proc.stderr)
    summary = _unity_summary(out)
    result = ProfileResult(id=profile.id, status=TOOL_FAILED,
                           command=" ".join(proc.args) if isinstance(proc.args, list) else "",
                           exit_code=proc.returncode, diagnostics=(err or out)[-4000:])
    docker_fault = docker_fault_reason(proc.returncode)
    if summary is None:
        if proc.returncode == 124:
            result.reason = "the profile run timed out before producing a Unity summary"
        elif docker_fault is not None:
            result.reason = f"{docker_fault}; no Unity summary was produced"
        elif "BUILD FAILURE" in out or "error:" in err.lower():
            result.reason = ("the profile build failed (no Unity summary); the "
                             "generated suite does not compile under this profile")
        else:
            result.reason = f"the profile produced no Unity summary (exit {proc.returncode})"
        return result
    total, failures, ignored = summary
    result.metrics = {"tests": total, "failures": failures, "ignored": ignored,
                      "executed": max(total - failures - ignored, 0), "build_root": build_root}
    if docker_fault is not None:
        result.status = TOOL_FAILED
        result.reason = f"{docker_fault} after a Unity summary"
        return result
    if proc.returncode != 0 and failures == 0 and not docker_fault:
        # A non-zero exit with no counted failure is not a test finding.
        result.status = TOOL_FAILED
        result.reason = (f"the runner exited {proc.returncode} although Unity reported "
                         "no failures")
        return result
    if failures:
        result.status = FAILED
        result.reason = f"{failures} assertion failure(s) under this profile"
    else:
        result.status = PASSED
        result.reason = "all tests passed"
    return result


def _run_functional(bench_root: Path, image_ref: str, profile: Profile,
                    run: CeedlingRun) -> ProfileResult:
    """Adapt the PR 2 Ceedling run to a :class:`ProfileResult`."""
    result = ProfileResult(id=profile.id, status=TOOL_FAILED,
                           command=" ".join(run.argv), exit_code=run.returncode,
                           diagnostics=run.diagnostics[-4000:],
                           raw_stdout=run.stdout, raw_stderr=run.stderr)
    if not run.ran:
        result.reason = ("the frozen suite produced no Unity summary"
                         + (f": {run.diagnostics.strip()[:200]}" if run.diagnostics else ""))
        return result
    result.metrics = {"tests": run.total, "failures": run.failed,
                      "ignored": run.ignored, "executed": run.executed,
                      "build_root": PROFILE_BUILD_ROOT["functional"],
                      # The parsed (test_function, reason) pairs, so a finding can
                      # name the failing assertion rather than a bare count.
                      "failures_detail": [list(pair) for pair in run.failures]}
    if run.timed_out:
        result.reason = "the run timed out after producing a Unity summary"
        return result
    result.status = FAILED if run.failed else PASSED
    result.reason = (f"{run.failed} assertion failure(s)" if run.failed else "all tests passed")
    return result


# --- coverage post-processing ----------------------------------------------


def _coverage_metrics(bench_root: Path, image_ref: str,
                      workdir: Path) -> dict[str, Any]:
    """Merge profraw files and export llvm-cov metrics (host-scoped).

    Returns the aggregated coverage summary plus per-file MC/DC counts; on any
    failure returns ``{"error": ...}`` so the caller marks the profile
    ``TOOL_FAILED`` rather than inventing a number.
    """
    cov_dir = workdir / "cov"
    profraws = sorted(cov_dir.glob("*.profraw"))
    if not profraws:
        return {"error": "no .profraw coverage data was produced by the run"}
    merge = _run_in_image(
        bench_root, image_ref, workdir,
        ["bash", "-lc",
         f"{_LLVM_PROFDATA} merge -sparse cov/*.profraw -o cov/total.profdata"],
        timeout=300)
    if merge.returncode != 0:
        return {"error": f"llvm-profdata failed: {_as_text(merge.stderr)[:400]}"}
    export = _run_in_image(
        bench_root, image_ref, workdir,
        ["bash", "-lc",
         # One executable per test file: export each and merge the summaries.
         "for exe in $(find . -name '*.out' -type f); do "
         f"  {_LLVM_COV} export \"$exe\" -instr-profile=cov/total.profdata -format=text; "
         "done"],
        timeout=300)
    if export.returncode != 0 or not export.stdout.strip():
        return {"error": f"llvm-cov export failed: {_as_text(export.stderr)[:400]}"}
    totals: dict[str, list[int]] = {"lines": [0, 0], "branches": [0, 0],
                                    "mcdc": [0, 0], "functions": [0, 0]}
    framework_totals: dict[str, list[int]] = {"lines": [0, 0], "branches": [0, 0],
                                              "mcdc": [0, 0], "functions": [0, 0]}
    per_file: dict[str, Any] = {}
    for line in export.stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        for entry in payload.get("data", []):
            for file_entry in entry.get("files", []):
                summary = file_entry.get("summary", {})
                fname = Path(file_entry.get("filename", "?")).name
                # Attribute the vendored framework (Unity/CMock and their generated
                # runners/mocks) to a *separate* denominator. Folding hundreds of
                # framework lines into the "coverage" percentage would misrepresent
                # how much of the *unit under test* was exercised — the metric the
                # DoD asks to be truthful about (denominators and applicability).
                bucket = framework_totals if _is_framework_file(fname) else totals
                rec: dict[str, Any] = {"scope": "framework" if bucket is framework_totals
                                       else "sut"}
                for key in totals:
                    part = summary.get(key, {})
                    bucket[key][0] += int(part.get("covered", 0))
                    bucket[key][1] += int(part.get("count", 0))
                    rec[key] = {"covered": part.get("covered", 0),
                                "count": part.get("count", 0),
                                "percent": part.get("percent", 0)}
                rec["mcdc_records"] = len(file_entry.get("mcdc_records", []))
                per_file[fname] = rec
    metrics: dict[str, Any] = {"per_file": per_file, "units": "host-instrumented",
                               "scope_note": ("'lines'/'branches'/'mcdc'/'functions' "
                                              "denominators cover the unit and its "
                                              "generated test only; vendored framework "
                                              "coverage is reported separately under "
                                              "'framework'")}
    for key, (covered, count) in totals.items():
        metrics[key] = {"covered": covered, "count": count,
                        "percent": round(100.0 * covered / count, 1) if count else None}
    metrics["framework"] = {
        key: {"covered": c, "count": n,
              "percent": round(100.0 * c / n, 1) if n else None}
        for key, (c, n) in framework_totals.items()}
    return metrics


#: Vendored framework translation units Ceedling compiles into every suite. Their
#: coverage is not evidence about the unit under test, so it is kept out of the
#: headline denominator.
_FRAMEWORK_FILES = {"unity.c", "unity.h", "unity_internals.h",
                    "cmock.c", "cmock.h", "cmock_internals.h"}


def _is_framework_file(name: str) -> bool:
    return (name in _FRAMEWORK_FILES or name.endswith("_runner.c")
            or name.startswith("mock_"))


# --- sanitizer / valgrind post-processing -----------------------------------


def _sanitizer_metrics(proc: subprocess.CompletedProcess) -> dict[str, Any]:
    """Parse ASan/UBSan diagnostics from a sanitizer run's combined output."""
    text = (_as_text(proc.stdout) + _as_text(proc.stderr))
    asan = "ERROR: AddressSanitizer" in text
    ubsan = ("runtime error:" in text) or ("UndefinedBehaviorSanitizer" in text)
    return {"asan_violation": asan, "ubsan_violation": ubsan,
            "violations": int(asan) + int(ubsan)}


def _valgrind_metrics(text: str) -> dict[str, Any]:
    """Parse Valgrind's ``ERROR SUMMARY`` and leak lines."""
    errors = None
    m = re.search(r"ERROR SUMMARY:\s*(\d+)\s+errors", text)
    if m:
        errors = int(m.group(1))
    leaks = None
    m = re.search(r"definitely lost:\s*([\d,]+)\s+bytes", text)
    if m:
        leaks = int(m.group(1).replace(",", ""))
    return {"errors": errors, "definitely_lost_bytes": leaks}


# --- top level --------------------------------------------------------------


def run_verify(bench_root: Path, selected: list[dict[str, Any]], *,
               image_ref: str, run_id: str | None = None,
               force: bool = False, runner=run_ceedling,
               profile_runner=None) -> VerifyResult:
    """Replay the frozen bench offline across the configured profiles.

    *runner* is the injected Ceedling executor for the functional profile (the PR 2
    runner by default); *profile_runner* executes a non-functional profile and
    defaults to the real in-image path. Both are injectable so the classification
    logic is testable without Docker.
    """
    frozen = check_frozen(bench_root, selected, force=force)
    run_id = run_id or f"verify-{datetime.now(timezone.utc):%Y%m%d}-{_short_id(bench_root)}"
    revision = _revision(bench_root)
    if not (revision / "project.yml").is_file():
        raise MtmError(
            code=Code.INPUT_MISSING,
            message="The frozen generated project is missing from the bench.",
            action="Run 'mtm generate' to (re)create the suite, then 'mtm verify'.",
            stage="verify",
        )
    evidence_dir = bench_root / VERIFY_STATE / run_id
    # A verify run is immutable: never reuse an existing run id's directory.
    if evidence_dir.exists():
        raise MtmError(
            code=Code.EVIDENCE_INVALID,
            message=f"Verify run {run_id!r} already exists; run records are immutable.",
            action="Choose another --run-id (or omit it for a fresh one).",
            stage="verify",
            input_reference=run_id,
        )

    workdirs = _write_profile_projects(bench_root)
    results: list[ProfileResult] = []
    toolchain: dict[str, Any] = {}

    # 1. Functional profile: the PR 2 Ceedling run (byte-identical suite).
    functional = PROFILES[0]
    if profile_runner is None or functional.id == "functional":
        try:
            cee = runner(bench_root, image_ref=image_ref)
            results.append(_run_functional(bench_root, image_ref, functional, cee))
        except MtmError:
            raise
        except Exception as exc:  # pragma: no cover - defensive: never lose a fault
            results.append(ProfileResult(id=functional.id, status=TOOL_FAILED,
                                         reason=f"the functional run could not start: {exc}"))

    # 2. Independent profiles. A profile that cannot run is UNSUPPORTED/TOOL_FAILED,
    #    never a silent pass; the others still run.
    for profile in PROFILES[1:]:
        runner_fn = profile_runner or _run_profile
        results.append(runner_fn(bench_root, image_ref, profile, workdirs))

    counts = _count(results)
    findings = _findings(results)
    status, exit_code = _overall(results)
    record = VerifyResult(
        status=status, exit_code=exit_code, run_id=run_id, profiles=results,
        counts=counts, snapshot_hash=frozen, findings=findings,
        evidence_dir=str(evidence_dir.relative_to(bench_root)) if evidence_dir else "",
        toolchain=toolchain,
    )
    _persist(bench_root, record, selected)
    return record


def _run_profile(bench_root: Path, image_ref: str, profile: Profile,
                 workdirs: dict[str, Path]) -> ProfileResult:
    """Execute one non-functional profile in the image."""
    missing = _have_tool(bench_root, image_ref, profile)
    if missing:
        return ProfileResult(id=profile.id, status=UNSUPPORTED,
                             reason=f"the tool {missing!r} is not available in the image",
                             limits=profile.limits)
    if profile.id == "valgrind":
        return _run_valgrind(bench_root, image_ref, profile, workdirs)
    return _run_instrumented(bench_root, image_ref, profile, workdirs)


def _run_instrumented(bench_root: Path, image_ref: str, profile: Profile,
                      workdirs: dict[str, Path]) -> ProfileResult:
    workdir = workdirs[profile.id]
    cov_env = None
    if profile.id == "coverage":
        (workdir / "cov").mkdir(exist_ok=True)
        cov_env = {"LLVM_PROFILE_FILE": f"{CONTAINER_BENCH}/{REVISION_DIRNAME.as_posix()}"
                                        f"/{PROFILE_DIRNAME}/{profile.id}/cov/%p.profraw"}
    proc = _run_in_image(bench_root, image_ref, workdir,
                         ["ceedling", "test:all"], env=cov_env, timeout=600)
    result = _classify_build_run(proc, profile, PROFILE_BUILD_ROOT.get(profile.id, ""))
    result.limits = profile.limits
    result.tool_versions = {profile.compiler: _tool_version(bench_root, image_ref, profile.compiler)} \
        if profile.compiler else {}
    if result.status in (PASSED, FAILED):
        if profile.id == "coverage":
            metrics = _coverage_metrics(bench_root, image_ref, workdir)
            if "error" in metrics:
                result.status = TOOL_FAILED
                result.reason = metrics["error"]
            else:
                result.metrics.update(metrics)
        elif profile.id == "sanitizers":
            result.metrics.update(_sanitizer_metrics(proc))
            if result.metrics.get("violations"):
                result.status = FAILED
                result.reason = "a sanitizer reported a runtime violation"
    # Preserve the raw profile output as evidence, carried on the result so
    # `_persist` writes it into the *run's* directory (never a shared path).
    result.raw_stdout = _as_text(proc.stdout)
    result.raw_stderr = _as_text(proc.stderr)
    return result


def _run_valgrind(bench_root: Path, image_ref: str, profile: Profile,
                  workdirs: dict[str, Path]) -> ProfileResult:
    """Build the gcc suite, then run each test binary under Valgrind Memcheck."""
    workdir = workdirs[profile.id]
    proc = _run_in_image(bench_root, image_ref, workdir,
                         ["ceedling", "test:all"], timeout=600)
    built = _classify_build_run(proc, profile, PROFILE_BUILD_ROOT["valgrind"])
    if built.status == TOOL_FAILED:
        built.limits = profile.limits
        return built
    vg = _run_in_image(
        bench_root, image_ref, workdir,
        ["bash", "-lc",
         "status=0; for exe in $(find . -name '*.out' -type f); do "
         "  valgrind --leak-check=full --error-exitcode=99 \"$exe\" || status=$?; "
         "done; echo VALGRIND_STATUS=$status"],
        timeout=900)
    text = _as_text(vg.stdout) + _as_text(vg.stderr)
    metrics = _valgrind_metrics(text)
    result = ProfileResult(
        id=profile.id,
        status=PASSED if metrics.get("errors") == 0 else FAILED,
        metrics=metrics, limits=profile.limits,
        command=" ".join(vg.args) if isinstance(vg.args, list) else "",
        exit_code=vg.returncode, diagnostics=text[-4000:],
        tool_versions={"valgrind": _tool_version(bench_root, image_ref, "valgrind")},
        raw_stdout=_as_text(vg.stdout), raw_stderr=_as_text(vg.stderr))
    if metrics.get("errors") is None:
        result.status = TOOL_FAILED
        result.reason = "Valgrind produced no ERROR SUMMARY line"
    elif metrics["errors"]:
        result.reason = f"Valgrind reported {metrics['errors']} error(s)"
    else:
        result.reason = "no memory error or leak reported"
    return result


def _save_raw(evidence_dir: Path, profile_id: str, stdout: str, stderr: str) -> None:
    """Write one profile's raw native output into the run's own directory.

    Scoped to the run (``<evidence_dir>/raw/``), never a shared location: two
    campaigns never overwrite each other's evidence, which is what makes a run
    record immutable and auditable.
    """
    directory = evidence_dir / "raw"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{profile_id}.out").write_text(stdout or "", encoding="utf-8")
    (directory / f"{profile_id}.err").write_text(stderr or "", encoding="utf-8")


def _short_id(bench_root: Path) -> str:
    import hashlib

    return hashlib.sha256(str(bench_root.resolve()).encode()).hexdigest()[:6]


def _count(results: list[ProfileResult]) -> dict[str, int]:
    counts = {PASSED: 0, FAILED: 0, UNSUPPORTED: 0, TOOL_FAILED: 0, NOT_APPLICABLE: 0}
    for result in results:
        counts[result.status] = counts.get(result.status, 0) + 1
    return counts


def _findings(results: list[ProfileResult]) -> list[dict[str, Any]]:
    """Distinct domain findings observed across the profiles.

    A finding is an **assertion failure** or a real **runtime/memory violation** —
    never a *tool* fault (a profile that produced no native report contributes
    nothing here). The functional profile is the primary source of assertion
    findings and carries the parsed ``(test, reason)`` pairs; a sanitizer profile
    that fails the **same** assertion is corroboration, not a second finding, so it
    is reported once. A sanitizer *runtime violation* (ASan/UBSan) and a Valgrind
    memory error are distinct findings in their own right.
    """
    findings: list[dict[str, Any]] = []
    seen: set[str] = set()
    functional_asserted = False

    def add(profile: str, category: str, observed: str, test: str = "") -> None:
        key = f"{category}|{test}|{observed}"
        if key in seen:
            return
        seen.add(key)
        findings.append({"profile": profile, "category": category,
                         "observed": observed, "test": test})

    # Assertion findings come from the functional profile (the conformity
    # baseline), which carries the parsed (test, reason) pairs. Another profile
    # (coverage, clang -O2) failing the *same* assertion is corroboration, not a
    # second finding — it would only inflate the count.
    for result in results:
        if result.status == FAILED and result.id == "functional":
            for test, reason in result.metrics.get("failures_detail", []) or []:
                add(result.id, "IMPLEMENTATION_DEFECT", reason or "assertion failed", test)
                functional_asserted = True
            if not result.metrics.get("failures_detail"):
                add(result.id, "IMPLEMENTATION_DEFECT", result.reason)
                functional_asserted = True
    # Distinct categories: a sanitizer runtime violation and a Valgrind memory
    # error are their own findings, independent of the functional suite.
    for result in results:
        if result.status != FAILED:
            continue
        if result.id == "sanitizers" and result.metrics.get("violations"):
            add(result.id, "RUNTIME_VIOLATION",
                "AddressSanitizer/UBSan reported a runtime violation")
        elif result.id == "valgrind":
            add(result.id, "MEMORY_ERROR", result.reason)
        elif result.id not in ("functional", "sanitizers"):
            # A non-functional profile that failed an assertion while the
            # functional profile somehow did not: report it once, generically, so
            # a real failure is never lost.
            if not functional_asserted:
                add(result.id, "IMPLEMENTATION_DEFECT",
                    f"an assertion failed under the {result.id} profile")
    return findings


def _overall(results: list[ProfileResult]) -> tuple[str, int]:
    """Aggregate profile outcomes into one honest operation status.

    Precedence (most severe operation-blocking first): an input/consistency fault
    is handled by raising before this point; a *tool* fault outranks a test
    finding (a broken profile cannot hide a co-observed defect, and a green test
    cannot hide a broken tool); unsupported profiles make the run PARTIAL; a
    genuine assertion failure is FINDINGS.
    """
    statuses = [r.status for r in results]
    if TOOL_FAILED in statuses:
        return "TOOL_FAILURE", 30
    if FAILED in statuses:
        return "FINDINGS", 10
    if UNSUPPORTED in statuses:
        return "PARTIAL", 20
    return "SUCCEEDED", 0


def _persist(bench_root: Path, record: VerifyResult,
             selected: list[dict[str, Any]]) -> None:
    """Write the immutable run record and its profile evidence."""
    evidence_dir = bench_root / record.evidence_dir
    evidence_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": 1,
        "run_id": record.run_id,
        "status": record.status,
        "exit_code": record.exit_code,
        "snapshot_hash": record.snapshot_hash,
        "selected": [{"path": e.get("path"), "kind": e.get("kind")} for e in selected],
        "counts": record.counts,
        "profiles": [p.to_dict() for p in record.profiles],
        "findings": record.findings,
        "toolchain": record.toolchain,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "note": "host-native evidence only; no target-MCU claim",
    }
    for profile in record.profiles:
        if profile.raw_stdout or profile.raw_stderr:
            _save_raw(evidence_dir, profile.id, profile.raw_stdout, profile.raw_stderr)
    write_json_atomic(evidence_dir / "status.json", payload)
    # The run index is a small append-only map, so 'mtm status --json' can point
    # at the most recent campaign without rescanning every evidence directory.
    index_path = bench_root / VERIFY_STATE / "index.json"
    index = read_json(index_path) or {}
    if not isinstance(index, dict):
        index = {}
    index[record.run_id] = {"status": record.status, "exit_code": record.exit_code,
                            "snapshot_hash": record.snapshot_hash,
                            "evidence_dir": record.evidence_dir}
    write_json_atomic(index_path, index)


def latest_run(bench_root: Path) -> dict[str, Any] | None:
    """The most recent verify run record, or ``None``."""
    index = read_json(bench_root / VERIFY_STATE / "index.json")
    if not isinstance(index, dict) or not index:
        return None
    run_id = sorted(index)[-1]
    return read_json(bench_root / VERIFY_STATE / run_id / "status.json")
