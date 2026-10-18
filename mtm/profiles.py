"""Profile definitions for offline verification (milestone 3, PR 3).

`mtm verify` replays a **frozen** generated bench without an LLM across a set of
independent *profiles*. This module owns the small, explicit data describing each
profile — what it is called, which toolchain it needs, which Ceedling build
directory and flags it uses, and how to detect that the profile does not apply to
this project (so it is reported as an honest non-applicability rather than an
invented pass).

Design rules (``docs/architecture/verification-strategy.md``):

* A profile is only `SUPPORTED` when its tool is actually present and the run
  produced its native report. A missing tool is `UNSUPPORTED` with its name; a
  run that produced no report is `TOOL_FAILED`, never `PASSED`.
* Profiles are independent: one failing or unsupported profile does not stop the
  others (``workflow-failures.md``: a failed profile is marked failed, the
  independent ones continue).
* No target-MCU claim is ever made: every metric is labelled *host*.

The functional profile (Ceedling/Unity/CMock) is the existing PR 2 runner; the
other profiles re-run the **same unchanged tests and sources** with a different
compiler/instrumentation, which is why they share the frozen revision directory.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# --- normalized profile outcomes --------------------------------------------

PASSED = "PASSED"
FAILED = "FAILED"
UNSUPPORTED = "UNSUPPORTED"
TOOL_FAILED = "TOOL_FAILED"
NOT_APPLICABLE = "NOT_APPLICABLE"

#: Where each profile's Ceedling build tree and raw artefacts live, relative to
#: the frozen revision directory (``<bench>/.mtm/gen``). One directory per
#: profile keeps an instrumented build from reusing another profile's objects.
PROFILE_BUILD_ROOT = {
    "functional": "build-functional",
    "coverage": "build-coverage",
    "sanitizers": "build-sanitizers",
    "compiler-clang": "build-clang",
    "valgrind": "build-valgrind",
}


@dataclass
class Profile:
    """One verification profile: a compiler/instrumentation variant of the suite.

    ``compiler`` is the tool that must be present for the profile to be
    `SUPPORTED` (``None`` = the image's default ``gcc``, always present). ``flags``
    are injected through the generated ``project.yml`` ``:flags:``/``:tools:``
    sections, so the *same* sources and tests are rebuilt — never edited.
    ``tool`` is the post-processing tool whose native report the profile needs.
    """

    id: str
    title: str
    compiler: str | None = None
    #: extra compile flags, and the link flags they imply.
    compile_flags: list[str] = field(default_factory=list)
    link_flags: list[str] = field(default_factory=list)
    #: absolute-path tool that must exist for the profile to run (``gcc`` etc.).
    requires: list[str] = field(default_factory=list)
    #: A human note on what the profile establishes and its limits.
    establishes: str = ""
    limits: str = ""


#: The profiles `verify` knows, in execution order. `functional` is the frozen
#: PR 2 suite and is always attempted first; it is the conformity baseline.
PROFILES: list[Profile] = [
    Profile(
        id="functional",
        title="Functional/robustness (Ceedling + Unity + CMock, gcc)",
        establishes="tested expected observations and mock contracts on the host",
        limits="oracles may be wrong; mocks differ from the real target",
    ),
    Profile(
        id="coverage",
        title="Structural coverage (clang source-based, incl. MC/DC)",
        compiler="clang-18",
        compile_flags=["-fprofile-instr-generate", "-fcoverage-mapping",
                       "-fcoverage-mcdc", "-O0", "-g", "-gdwarf-4"],
        link_flags=["-fprofile-instr-generate"],
        requires=["clang-18", "llvm-profdata-18", "llvm-cov-18"],
        establishes="instrumented exercised regions/lines/branches and supported "
                    "MC/DC decisions, with raw llvm-cov evidence",
        limits="coverage != correctness; unsupported/unreachable conditions; "
               "host instrumentation only, never an MCU claim",
    ),
    Profile(
        id="sanitizers",
        title="Dynamic runtime (clang ASan + UBSan)",
        compiler="clang-18",
        compile_flags=["-fsanitize=address,undefined", "-fno-omit-frame-pointer",
                       "-O1", "-g"],
        link_flags=["-fsanitize=address,undefined"],
        requires=["clang-18"],
        establishes="no ASan/UBSan violation observed on the executed paths",
        limits="only executed paths; ASan is not LSan/TSan; no concurrency case",
    ),
    Profile(
        id="compiler-clang",
        title="Host portability (clang-18, -O2, warnings as errors)",
        compiler="clang-18",
        compile_flags=["-O2", "-g", "-gdwarf-4", "-Wall", "-Wextra"],
        link_flags=[],
        requires=["clang-18"],
        establishes="the same tests pass under a second (clang) compiler and -O2",
        limits="not the target ABI; -O2 may expose nothing on this small scope",
    ),
    Profile(
        id="valgrind",
        title="Memory errors/leaks (Valgrind Memcheck on the gcc build)",
        compiler=None,
        requires=["valgrind"],
        establishes="no invalid memory access or leak observed under Memcheck",
        limits="slow; only host behaviour; conditional on an executable profile "
               "having built a runnable suite",
    ),
]
