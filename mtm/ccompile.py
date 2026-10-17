"""Host C compilation helpers for feasibility and generated-suite execution.

A thin, explicit wrapper over the system C compiler (``cc``/``gcc``). It never
edits inputs: it compiles the *original* translation units, adding only include
directories and generated support files, and runs the produced binary in a
temporary directory. All diagnostics are preserved verbatim as evidence.

In this milestone the compile/run happens with the host compiler; the pinned
in-image deterministic toolchain (Clang/LLVM, sanitizers, Valgrind) is the PR 3
deliverable. Nothing here claims target-MCU behaviour: it is host-native
compilation, reported as such (``docs/architecture/verification-strategy.md``).
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class CompileResult:
    """Outcome of one compiler/link invocation."""

    ok: bool
    argv: list[str]
    stdout: str = ""
    stderr: str = ""
    returncode: int = 0

    @property
    def diagnostics(self) -> str:
        return (self.stderr or self.stdout).strip()


@dataclass
class RunResult:
    """Outcome of running a produced binary."""

    ok: bool
    returncode: int
    stdout: str = ""
    stderr: str = ""
    argv: list[str] = field(default_factory=list)


def find_compiler() -> str | None:
    """Return an available host C compiler name, or None."""
    for name in ("cc", "gcc", "clang"):
        if shutil.which(name):
            return name
    return None


class HostCompiler:
    """Compile and run C sources with the host compiler."""

    def __init__(self, compiler: str | None = None, *, timeout: int = 60):
        self.compiler = compiler or find_compiler()
        self.timeout = timeout

    @property
    def available(self) -> bool:
        return self.compiler is not None

    def syntax_check(self, source: Path, include_dirs: list[Path]) -> CompileResult:
        """Compile-only (syntax/semantic) check of a single source file."""
        return self._run(["-fsyntax-only"], source, include_dirs)

    def link(
        self,
        sources: list[Path],
        output: Path,
        include_dirs: list[Path],
        *,
        extra_flags: list[str] | None = None,
    ) -> CompileResult:
        """Compile and link *sources* into the executable *output*."""
        if not self.available:
            return CompileResult(ok=False, argv=[], stderr="no host C compiler available")
        argv = [self.compiler, "-std=c11", "-g", "-O0"]
        for inc in include_dirs:
            argv += ["-I", str(inc)]
        argv += list(extra_flags or [])
        argv += [str(s) for s in sources]
        argv += ["-o", str(output)]
        return self._exec(argv)

    def run(self, binary: Path, *, args: list[str] | None = None, cwd: Path | None = None) -> RunResult:
        argv = [str(binary), *(args or [])]
        try:
            proc = subprocess.run(
                argv, capture_output=True, text=True, timeout=self.timeout,
                check=False, cwd=str(cwd) if cwd else None,
            )
        except subprocess.TimeoutExpired as exc:
            return RunResult(ok=False, returncode=124, stderr=f"timeout after {self.timeout}s",
                             argv=argv, stdout=(exc.stdout or ""))
        return RunResult(ok=proc.returncode == 0, returncode=proc.returncode,
                         stdout=proc.stdout, stderr=proc.stderr, argv=argv)

    # -- internals ----------------------------------------------------------

    def _run(self, flags: list[str], source: Path, include_dirs: list[Path]) -> CompileResult:
        if not self.available:
            return CompileResult(ok=False, argv=[], stderr="no host C compiler available")
        argv = [self.compiler, "-std=c11", *flags]
        for inc in include_dirs:
            argv += ["-I", str(inc)]
        argv.append(str(source))
        return self._exec(argv)

    def _exec(self, argv: list[str]) -> CompileResult:
        try:
            proc = subprocess.run(argv, capture_output=True, text=True,
                                  timeout=self.timeout, check=False)
        except subprocess.TimeoutExpired as exc:
            return CompileResult(ok=False, argv=argv, returncode=124,
                                 stderr=f"compiler timeout after {self.timeout}s",
                                 stdout=(exc.stdout or ""))
        except OSError as exc:
            return CompileResult(ok=False, argv=argv, returncode=127, stderr=str(exc))
        return CompileResult(ok=proc.returncode == 0, argv=argv,
                             stdout=proc.stdout, stderr=proc.stderr, returncode=proc.returncode)


def make_workdir(prefix: str = "mtm-build-") -> tempfile.TemporaryDirectory:
    """A temporary build directory, outside the bench and the inputs."""
    return tempfile.TemporaryDirectory(prefix=prefix)
