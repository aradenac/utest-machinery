"""Selected build-context and native mock preprocessing regression checks.

The context used by clang must be the context used by the executable Ceedling
project; unsupported per-TU flags cannot be silently lost in generation.
"""
import json
from pathlib import Path

import pytest

from mtm.build_context import context_identity, resolve_build_context
from mtm.errors import Code, MtmError
from mtm.gen import _project_yml
from mtm.profiles import PROFILES
from mtm.verify import _profile_project_yml


def test_explicit_includes_defines_and_abi_reach_ceedling_and_profile(tmp_path):
    (tmp_path / "include").mkdir()
    (tmp_path / "include" / "switch.h").write_text(
        "#if defined(FEATURE_X)\nint dep(int x);\n#endif\n")
    source = tmp_path / "use.c"
    source.write_text('#include "switch.h"\nint use(int x) { return dep(x); }\n')
    cfg = {"include_dirs": ["include"], "defines": ["FEATURE_X=1"],
           "args": ["-std=c11", "-m64"], "mock_preprocessor": "auto"}
    ctx = resolve_build_context(tmp_path, cfg, [source])
    assert list(ctx.defines) == ["FEATURE_X=1"]
    assert list(ctx.args) == ["-std=c11", "-m64"]
    assert ctx.include_dirs == (tmp_path / "include",)
    yml = _project_yml(ctx, bench_root=tmp_path,
                       mock_headers=[tmp_path / "include" / "switch.h"])
    assert ":use_test_preprocessor: :mocks" in yml
    assert "../../include/**" in yml
    assert ":test_preprocess:" in yml and "FEATURE_X=1" in yml
    assert "- -std=c11" in yml and "- -m64" in yml
    coverage = next(p for p in PROFILES if p.id == "coverage")
    derived = _profile_project_yml(yml, coverage)
    import yaml
    config = yaml.safe_load(derived)
    assert "../../../../include/**" in config[":paths"][":include"]
    assert "FEATURE_X=1" in config[":defines"][":test"]
    assert "FEATURE_X=1" in config[":defines"][":test_preprocess"]
    assert "-std=c11" in config[":flags"][":test"][":compile"][":*"]
    assert "-m64" in config[":flags"][":test"][":link"][":*"]
    assert "-fcoverage-mcdc" in config[":flags"][":test"][":compile"][":*"]


def test_no_conditional_header_keeps_preprocessing_off(tmp_path):
    plain = tmp_path / "plain.h"
    plain.write_text("int dep(int x);\n")
    ctx = resolve_build_context(tmp_path, {}, [])
    assert ":use_test_preprocessor: :none" in _project_yml(
        ctx, bench_root=tmp_path, mock_headers=[plain])
    forced = resolve_build_context(tmp_path, {"mock_preprocessor": "mocks"}, [])
    assert ":use_test_preprocessor: :mocks" in _project_yml(
        forced, bench_root=tmp_path, mock_headers=[plain])


def test_explicit_selected_compilation_database_applies_uniform_flags(tmp_path):
    inc = tmp_path / "inc"
    inc.mkdir()
    c = tmp_path / "sensor.c"
    c.write_text("int sensor(void) { return 1; }\n")
    db = [{"directory": str(tmp_path), "file": "sensor.c",
           "arguments": ["cc", "-c", "-std=c11", "-DFEATURE=1",
                         "-I", "inc", "-o", "sensor.o", "sensor.c"]}]
    (tmp_path / "compile_commands.json").write_text(json.dumps(db))
    ctx = resolve_build_context(
        tmp_path, {"compile_commands": "compile_commands.json"}, [c])
    assert ctx.include_dirs == (inc,)
    assert ctx.defines == ("FEATURE=1",)
    assert ctx.args == ("-std=c11",)
    assert context_identity(tmp_path, {"compile_commands": "compile_commands.json"})


def test_conflicting_compilation_database_is_rejected_not_guessed(tmp_path):
    src = []
    for name in ("a.c", "b.c"):
        p = tmp_path / name
        p.write_text("int a(void){return 1;}\n")
        src.append(p)
    db = [{"directory": str(tmp_path), "file": p.name,
           "arguments": ["cc", "-c", f"-DFEATURE={i}", p.name]}
          for i, p in enumerate(src)]
    (tmp_path / "compile_commands.json").write_text(json.dumps(db))
    with pytest.raises(MtmError) as err:
        resolve_build_context(
            tmp_path, {"compile_commands": "compile_commands.json"}, src)
    assert err.value.code == Code.CONFIG_INVALID
    assert "per-unit" in err.value.message


def test_unsupported_abi_or_missing_context_is_explicit(tmp_path):
    c = tmp_path / "main.c"
    c.write_text("int main(void){return 0;}\n")
    with pytest.raises(MtmError) as err:
        resolve_build_context(tmp_path, {"args": ["--target=arm-none-eabi"]}, [c])
    assert err.value.code == Code.CONFIG_INVALID and "target" in err.value.message
    with pytest.raises(MtmError) as err:
        resolve_build_context(tmp_path, {"include_dirs": ["/opt/vendor-sdk"]}, [c])
    assert err.value.code == Code.CONFIG_INVALID


def test_compilation_database_bytes_participate_in_replay_identity(tmp_path):
    db = tmp_path / "compile_commands.json"
    db.write_text("[]\n")
    cfg = {"compile_commands": "compile_commands.json"}
    before = context_identity(tmp_path, cfg)
    db.write_text('[{"file":"a.c"}]\n')
    assert context_identity(tmp_path, cfg) != before
