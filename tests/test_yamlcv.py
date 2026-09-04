"""Tests for resumes/yamlcv.py — the 1-page render/re-iterate loop."""
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pymupdf

from resumes import yamlcv


def make_pdf(path: Path, pages: int):
    doc = pymupdf.open()
    for _ in range(pages):
        doc.new_page()
    doc.save(str(path))
    doc.close()


class FakeLLM:
    """Returns YAML; the 'second' draft is shorter (simulates trimming)."""
    def __init__(self, pages_sequence):
        self.pages_sequence = list(pages_sequence)
        self.calls = 0

    def chat(self, messages):
        self.calls += 1
        body = "cv:\n  name: Juan Garcia\n  sections:\n    profile:\n      - x\n"
        if self.calls >= 2:
            body += "  design:\n    theme: harvard\n"
        return f"```yaml\n{body}```"


def fake_run_factory(pages_sequence):
    """subprocess.run stub: writes a real PDF with the next page count."""
    it = iter(pages_sequence)

    def run(cmd, **kw):
        pages = next(it, 1)
        out = Path(kw["cwd"]) / "rendercv_output"
        out.mkdir(parents=True, exist_ok=True)
        make_pdf(out / "cv.pdf", pages)

        class R:
            returncode = 0
            stdout = "ok"
            stderr = ""
        return R()
    return run


def test_two_rounds_until_one_page(tmp_path, monkeypatch):
    monkeypatch.setattr(yamlcv, "RENDERCV_BIN", Path("rendercv.exe"))
    monkeypatch.setattr(yamlcv.subprocess, "run",
                        fake_run_factory([2, 3, 1]))
    llm = FakeLLM([2, 3, 1])
    r = yamlcv.build_one_pager(llm, "CORPUS", "IDENTITY", "JOB", tmp_path,
                               max_rounds=5)
    assert r["ok"] and r["pages"] == 1
    assert r["rounds"] == 3          # 2 overflows, then success
    assert llm.calls == 3
    assert [h["pages"] for h in r["history"]] == [2, 3, 1]
    assert Path(r["pdf"]).exists()


def test_gives_up_after_max_rounds(tmp_path, monkeypatch):
    monkeypatch.setattr(yamlcv, "RENDERCV_BIN", Path("rendercv.exe"))
    monkeypatch.setattr(yamlcv.subprocess, "run",
                        fake_run_factory([2, 2, 2, 2]))
    llm = FakeLLM([2, 2, 2, 2])
    r = yamlcv.build_one_pager(llm, "CORPUS", "I", "J", tmp_path, max_rounds=3)
    assert not r["ok"] and r["rounds"] == 3
    assert "one page" in r["error"]


def test_render_error_feeds_back(tmp_path, monkeypatch):
    monkeypatch.setattr(yamlcv, "RENDERCV_BIN", Path("rendercv.exe"))

    def run(cmd, **kw):
        if getattr(run, "n", 0) == 0:
            run.n = 1
            class R:
                returncode = 1
                stdout = ""
                stderr = "Error: extra keys not allowed (section: skills)"
            return R
        out = Path(kw["cwd"]) / "rendercv_output"
        out.mkdir(parents=True, exist_ok=True)
        make_pdf(out / "cv.pdf", 1)

        class R2:
            returncode = 0
            stdout = "ok"
            stderr = ""
        return R2
    monkeypatch.setattr(yamlcv.subprocess, "run", run)
    llm = FakeLLM([1, 1])
    r = yamlcv.build_one_pager(llm, "C", "I", "J", tmp_path)
    assert r["ok"] and r["rounds"] == 2
    assert "extra keys" in r["history"][0]["error"]


def test_fenced_yaml_extracts_block():
    assert yamlcv._fenced_yaml("junk\n```yaml\na: 1\n```\ntail") == "a: 1"
    assert yamlcv._fenced_yaml("```yml\na: 1\n```") == "a: 1"
    assert yamlcv._fenced_yaml("plain: 1") == "plain: 1"


def test_available_reflects_binary(tmp_path, monkeypatch):
    monkeypatch.setattr(yamlcv, "RENDERCV_BIN", tmp_path / "nope.exe")
    assert not yamlcv.available()
    f = tmp_path / "rc.exe"
    f.write_text("x")
    monkeypatch.setattr(yamlcv, "RENDERCV_BIN", f)
    assert yamlcv.available()
