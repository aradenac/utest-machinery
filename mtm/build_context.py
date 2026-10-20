"""Selected host C compilation context shared by libclang and Ceedling.

This module translates only the common, reproducible host-C options. It does
NOT emulate a target toolchain or silently ignore options changing C semantics.
A compilation database may be selected explicitly; heterogeneous per-TU
contexts are rejected because the current Ceedling bench has one C toolchain.
"""
from __future__ import annotations

import hashlib
import json
import re
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .errors import Code, MtmError


@dataclass(frozen=True)
class BuildContext:
    include_dirs: tuple[Path, ...] = ()
    defines: tuple[str, ...] = ()
    args: tuple[str, ...] = ()
    mock_preprocessor: str = "auto"

    def compile_flags(self) -> list[str]:
        return [*(f"-I{p}" for p in self.include_dirs),
                *(f"-D{d}" for d in self.defines), *self.args]


def _invalid(message: str) -> MtmError:
    return MtmError(code=Code.CONFIG_INVALID, stage="generate",
                    message=f"Selected C build context: {message}",
                    action="Correct build_context in project.yaml or select a "
                           "supported host compilation command.")


def _inside(root: Path, value: str, *, directory: Path | None = None) -> Path:
    target = Path(value)
    if not target.is_absolute():
        target = (directory or root) / target
    target = target.resolve()
    if not target.is_relative_to(root.resolve()):
        raise _invalid(f"path {str(value)!r} escapes the selected bench")
    return target


def _string_list(value: Any, label: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list) or any(not isinstance(v, str) or not v for v in value):
        raise _invalid(f"{label} must be a list of nonempty strings")
    return value


def _flags(tokens: list[str], directory: Path, root: Path) -> tuple[list[Path], list[str], list[str]]:
    includes: list[Path] = []
    defines: list[str] = []
    args: list[str] = []
    i = 0
    while i < len(tokens):
        arg = tokens[i]
        if arg in ("-I", "-D", "-U", "-std", "-isystem"):
            if i + 1 == len(tokens):
                raise _invalid(f"missing value for compiler option {arg}")
            i += 1
            value = tokens[i]
            arg = arg + value if arg != "-std" else "-std=" + value
        if arg.startswith("-I") and len(arg) > 2:
            folder = _inside(root, arg[2:], directory=directory)
            if not folder.is_dir():
                raise _invalid(f"include root {folder} is missing")
            includes.append(folder)
        elif arg.startswith("-isystem") and len(arg) > 8:
            folder = _inside(root, arg[8:], directory=directory)
            if not folder.is_dir():
                raise _invalid(f"system include root {folder} is missing")
            includes.append(folder)
        elif arg.startswith("-D") and len(arg) > 2:
            if not re.fullmatch(r"[A-Za-z_]\w*(?:=.*)?", arg[2:]):
                raise _invalid(f"invalid macro definition {arg}")
            defines.append(arg[2:])
        elif arg.startswith("-U") and len(arg) > 2:
            if not re.fullmatch(r"[A-Za-z_]\w*", arg[2:]):
                raise _invalid(f"invalid macro undefinition {arg}")
            args.append(arg)
        elif arg.startswith("-std=") or arg in ("-m32", "-m64", "-fshort-enums",
                                                  "-funsigned-char", "-fsigned-char"):
            args.append(arg)
        elif arg in ("-c", "-pipe") or re.fullmatch(r"-(?:O[0-3sgz]|g\d*|W[\w=-]+)", arg):
            pass  # host-only diagnostic/optimization; verification profiles own optimization
        elif arg == "-o":
            i += 1
            if i >= len(tokens):
                raise _invalid("missing output after -o")
        elif arg.startswith("-o") and len(arg) > 2:
            pass
        elif arg in ("-target", "--target") or arg.startswith(("--target=", "-mcpu=", "-march=")):
            raise _invalid(f"target-only option {arg!r} cannot be replayed by the host gcc/Clang bench")
        elif arg.startswith("-"):
            raise _invalid(f"unsupported compiler option {arg!r}; refusing to guess its semantics")
        else:
            # The compilation database's source file (or compiler executable)
            # has already been identified; other positionals are ambiguous.
            raise _invalid(f"unexpected compilation argument {arg!r}")
        i += 1
    return includes, defines, args


def _database_flags(root: Path, dbfile: Path, source_paths: list[Path]) -> tuple[list[Path], list[str], list[str]]:
    try:
        records = json.loads(dbfile.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise _invalid(f"cannot read compilation database {dbfile}: {exc}") from exc
    if not isinstance(records, list):
        raise _invalid("compile_commands.json must be a JSON array")
    # Use the command for every selected translation unit; a silently unmatched
    # unit or different per-TU macro value would make the generated bench lie.
    by_file: dict[Path, tuple[list[Path], list[str], list[str]]] = {}
    for entry in records:
        if not isinstance(entry, dict):
            raise _invalid("malformed compilation database entry")
        directory = _inside(root, str(entry.get("directory") or "."))
        src = _inside(root, str(entry.get("file") or ""), directory=directory)
        if not src.is_file():
            continue
        if "arguments" in entry:
            argv = _string_list(entry["arguments"], "compile command arguments")
        else:
            command = entry.get("command")
            if not isinstance(command, str):
                raise _invalid(f"{src}: missing command/arguments")
            argv = shlex.split(command)
        if not argv:
            raise _invalid(f"{src}: empty compilation command")
        # Compiler binary is not an option. The selected C source is replaced
        # with the private copied TU; only semantic flags are replayed.
        options: list[str] = []
        for token in argv[1:]:
            if token.startswith("-"):
                options.append(token)
            elif token == str(entry.get("file")) or _inside(root, token, directory=directory) == src:
                continue
            else:
                options.append(token)
        normalized = _flags(options, directory, root)
        if src in by_file and by_file[src] != normalized:
            raise _invalid(f"multiple incompatible compile commands for {src}")
        by_file[src] = normalized
    scoped = [p.resolve() for p in source_paths if p.suffix == ".c"]
    if not scoped:
        return [], [], []
    missing = [p for p in scoped if p not in by_file]
    if missing:
        raise _invalid(f"compile_commands.json has no command for {missing[0]}")
    first = by_file[scoped[0]]
    if any(by_file[p] != first for p in scoped[1:]):
        raise _invalid("per-unit compiler flags differ; a single generated "
                       "Ceedling project cannot faithfully replay those contexts")
    return first


def resolve_build_context(bench_root: Path, config: dict[str, Any] | None,
                          source_paths: list[Path]) -> BuildContext:
    """Resolve only explicitly supplied context, fail closed on invalid settings."""
    root = bench_root.resolve()
    config = config or {}
    if not isinstance(config, dict):
        raise _invalid("build_context must be a mapping")
    unknown = set(config) - {"include_dirs", "defines", "args",
                             "compile_commands", "mock_preprocessor"}
    if unknown:
        raise _invalid(f"unsupported keys: {', '.join(sorted(unknown))}")
    directory = root
    includes: list[Path] = []
    for raw in _string_list(config.get("include_dirs"), "include_dirs"):
        folder = _inside(root, raw)
        if not folder.is_dir():
            raise _invalid(f"include root {raw!r} is missing")
        includes.append(folder)
    defines = _string_list(config.get("defines"), "defines")
    for value in defines:
        if not re.fullmatch(r"[A-Za-z_]\w*(?:=.*)?", value):
            raise _invalid(f"invalid macro definition {value!r}")
    explicit_args = _string_list(config.get("args"), "args")
    extra_inc, extra_def, args = _flags(explicit_args, directory, root)
    includes += extra_inc
    defines += extra_def
    database = config.get("compile_commands")
    if database:
        if not isinstance(database, str):
            raise _invalid("compile_commands must be a path string")
        dbfile = _inside(root, database)
        if not dbfile.is_file():
            raise _invalid(f"compilation database {database!r} is missing")
        db_inc, db_def, db_args = _database_flags(root, dbfile, source_paths)
        includes += db_inc
        defines += db_def
        args += db_args
    preprocessor = config.get("mock_preprocessor", "auto")
    if preprocessor not in ("auto", "mocks", "none"):
        raise _invalid("mock_preprocessor must be auto, mocks or none")
    if len(set(defines)) != len(defines):
        defines = list(dict.fromkeys(defines))
    return BuildContext(tuple(dict.fromkeys(includes)), tuple(defines),
                        tuple(dict.fromkeys(args)), preprocessor)


def context_identity(root: Path, config: dict[str, Any] | None) -> str:
    """Hash the selected settings plus compilation DB bytes for frozen replay."""
    config = config or {}
    if not isinstance(config, dict):
        raise _invalid("build_context must be a mapping")
    dbfile = config.get("compile_commands")
    dbhash = ""
    if dbfile:
        path = _inside(root.resolve(), str(dbfile))
        if not path.is_file():
            raise _invalid(f"compilation database {dbfile!r} is missing")
        dbhash = hashlib.sha256(path.read_bytes()).hexdigest()
    blob = json.dumps({"settings": config, "database_sha256": dbhash},
                      sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(blob).hexdigest()
