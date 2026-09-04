"""Tests for resumes/digest.py — multi-file fact corpus."""
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pymupdf

from resumes import digest


def make_pdf(path: Path, text: str):
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((50, 70), text)
    doc.save(str(path))
    doc.close()


@pytest.fixture
def sources(tmp_path):
    d = tmp_path / "resumes"
    d.mkdir()
    (d / "master.txt").write_text(
        "Jeyson Dagondon\n"
        "Email: a@b.com\n"
        "SKILLS\n"
        "Pharmacy automation. SQL data pipelines.\n"
        "EXPERIENCE\n"
        "Pharmacy Tech - Salhab Pharmacy\n"
        "- Built an SMS dosing parser cutting manual entry by 40%\n"
        "- Managed 200+ daily prescriptions with zero errors\n"
        "- Built an SMS dosing parser cutting manual entry by 40%\n"
        "EDUCATION\n"
        "BSc Pharmacy, Liceo de Cagayan University, 2025\n",
        encoding="utf-8",
    )
    make_pdf(d / "tech.pdf", "Python. SQL. Python again. ETL pipelines for healthcare data.")
    (d / "report.png").write_bytes(b"\x89PNG fake")
    (d / "notes.txt").write_text("- Automated Epic export scripts saving 3 hours weekly\n",
                                 encoding="utf-8")
    return [str(d)]


def test_digest_extracts_all_files(sources):
    c = digest.digest(sources)
    files = [Path(s["file"]).name for s in c["sources"]]
    assert "master.txt" in files and "tech.pdf" in files and "notes.txt" in files
    assert c["chars"] > 100
    assert c["images"] and "report.png" in c["images"][0]


def test_bullets_deduped(sources):
    c = digest.digest(sources)
    assert c["bullets"].count("Built an SMS dosing parser cutting manual entry by 40%") == 1
    assert "Automated Epic export scripts saving 3 hours weekly" in c["bullets"]


def test_terms_are_skill_hints(sources):
    c = digest.digest(sources)
    assert "python" in c["terms"]
    # common glue words are filtered
    assert "the" not in c["terms"]


def test_corpus_text_has_markers_and_caps(sources):
    t = digest.corpus_text(digest.digest(sources), cap=2000)
    assert "=== SOURCE:" in t and "=== ALL BULLET POINTS" in t
    assert len(t) <= 2000
    assert len(digest.corpus_text(digest.digest(sources), cap=50)) <= 50


def test_collect_expands_and_orders_masters_first(tmp_path):
    (tmp_path / "zz.txt").write_text("x")
    (tmp_path / "masters.json").write_text("{}")
    files = digest.collect_files([str(tmp_path)])
    assert files[0].name == "masters.json"


def test_missing_source_is_ignored(tmp_path):
    c = digest.digest([str(tmp_path / "nope")])
    assert c["sources"] == [] and c["chars"] == 0
