"""Guardrails for files that become visible when the repository is public."""

from pathlib import Path
import subprocess


REPO_ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN_TEXT = (
    "ben" + "ya",
    "ben" + "yah@gmail.com",
    "tail" + "01b632.ts.net",
    "/home/" + "xerial",
    "G:" + "\\My Drive",
)


def tracked_text_files():
    names = subprocess.check_output(
        ["git", "ls-files"], cwd=REPO_ROOT, text=True
    ).splitlines()
    for name in names:
        path = REPO_ROOT / name
        try:
            yield name, path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue


def test_tracked_files_do_not_expose_private_machine_details():
    findings = []
    for name, text in tracked_text_files():
        for forbidden in FORBIDDEN_TEXT:
            if forbidden.casefold() in text.casefold():
                findings.append(f"{name}: {forbidden}")
    assert findings == []


def test_local_secrets_and_browser_artifacts_are_ignored():
    ignore = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
    for pattern in (".env", "*.key", "*.pem", ".playwright-mcp/"):
        assert pattern in ignore


def test_readme_explains_external_data_and_analysis_dependencies():
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    assert "https://github.com/galfthan/mvd_analyzer" in readme
    assert "https://github.com/Xerialen/mvd_analyzer" in readme
    assert "https://github.com/vikpe/mvd-api" not in readme
    assert "Xerial's private stats database" in readme
    assert "replaceable discovery adapter" in readme


def test_public_tree_excludes_internal_agent_configuration():
    internal_paths = (
        "AGENTS.md",
        "CLAUDE.md",
        "codex.md",
        "reviewer.md",
        "machine-learning-reviewer.md",
        "agents",
        "prompts",
        ".github/REVIEW_GATE.md",
        ".github/workflows/review-gate-merge.yml",
    )
    assert [path for path in internal_paths if (REPO_ROOT / path).exists()] == []
