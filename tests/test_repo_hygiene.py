"""
tests/test_repo_hygiene.py — W1.3 repo hygiene invariants.
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_no_license_file_claiming_a_license():
    assert not (ROOT / "LICENSE").exists()
    assert not (ROOT / "LICENSE.txt").exists()


def test_readme_states_all_rights_reserved():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "All rights reserved" in readme


def test_security_and_editorconfig_present():
    assert (ROOT / "SECURITY.md").exists()
    assert (ROOT / ".editorconfig").exists()


def test_issue_templates_present():
    assert (ROOT / ".github" / "ISSUE_TEMPLATE" / "bug_report.md").exists()
    assert (ROOT / ".github" / "ISSUE_TEMPLATE" / "feature_request.md").exists()


def test_pre_push_hook_wired_to_gate():
    hook = ROOT / ".githooks" / "pre-push"
    assert hook.exists()
    assert "tools/gate.sh" in hook.read_text(encoding="utf-8")
