"""docs/configuration.md must match DEFAULT_CONFIG in config.py."""

import importlib.util
import os

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture
def gen(tmp_path, monkeypatch):
    # The generator only parses config.py, but keep any accidental import of
    # config away from the real user-data folder.
    monkeypatch.delenv("APPDATA", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    spec = importlib.util.spec_from_file_location(
        "gen_config_docs", os.path.join(ROOT, "tools", "gen_config_docs.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _source():
    with open(os.path.join(ROOT, "config.py"), encoding="utf-8") as f:
        return f.read()


def test_doc_is_up_to_date(gen):
    assert gen.main(["--check"]) == 0


def test_every_default_key_is_documented(gen):
    with open(os.path.join(ROOT, "docs", "configuration.md"), encoding="utf-8") as f:
        doc = f.read()
    for name, _, _ in gen.read_defaults(_source()):
        assert f"| `{name}` |" in doc


def test_new_key_makes_check_fail(gen, tmp_path, monkeypatch):
    changed = _source().replace(
        '"first_run_complete": False,',
        '"first_run_complete": False,\n    "brand_new_key": 1,  # added in a test')
    assert "brand_new_key" in gen.render(changed)
    config_copy = tmp_path / "config.py"
    config_copy.write_text(changed, encoding="utf-8")
    monkeypatch.setattr(gen, "CONFIG_PY", str(config_copy))
    assert gen.main(["--check"]) == 1


def test_multiline_value_and_pipe_in_default(gen):
    changed = _source().replace(
        '"first_run_complete": False,',
        '"first_run_complete": False,\n'
        '    "spread_key": [  # first part\n'
        '        "a|b",       # second part\n'
        '    ],')
    row = next(line for line in gen.render(changed).splitlines() if "`spread_key`" in line)
    assert row == '| `spread_key` | `["a\\|b"]` | first part second part |'
