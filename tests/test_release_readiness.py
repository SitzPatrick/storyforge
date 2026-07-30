from __future__ import annotations

import os
import subprocess
import sys
import tomllib
from pathlib import Path

import app
import storyforge
from app.config import load_settings
from storyforge.release_check import collect_findings

REPO_ROOT = Path(__file__).resolve().parents[1]


def _run_storyforge(*args: str, cwd: Path | None = None) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env.setdefault("PYTHONPATH", str(REPO_ROOT))
    return subprocess.run(
        [sys.executable, "-m", "storyforge", *args],
        cwd=str(cwd or REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _init_git_repo(root: Path) -> None:
    subprocess.run(["git", "init"], cwd=root, check=True, capture_output=True, text=True)
    subprocess.run(
        ["git", "config", "user.email", "tester@example.com"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Tester"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    )


def _write_minimal_release_files(root: Path, *, include_readme: bool = True) -> None:
    (root / "pyproject.toml").write_text(
        """
        [build-system]
        requires = ["setuptools>=69"]
        build-backend = "setuptools.build_meta"

        [project]
        name = "storyforge"
        version = "0.1.0a1"
        readme = "README.md"
        license = { file = "LICENSE" }
        requires-python = ">=3.11"
        scripts = { storyforge = "storyforge.cli:main" }
        """.strip() + "\n",
        encoding="utf-8",
    )
    (root / "LICENSE").write_text("license\n", encoding="utf-8")
    (root / "CHANGELOG.md").write_text("# Changelog\n", encoding="utf-8")
    (root / "CONTRIBUTING.md").write_text("# Contributing\n", encoding="utf-8")
    (root / "SECURITY.md").write_text("# Security\n", encoding="utf-8")
    (root / "CODE_OF_CONDUCT.md").write_text("# CoC\n", encoding="utf-8")
    (root / ".gitignore").write_text("*.log\n", encoding="utf-8")
    (root / "config").mkdir(parents=True, exist_ok=True)
    (root / "config" / "config.yaml").write_text(
        (
            "paths: {}\n"
            "kokoro: {}\n"
            "conversion: {}\n"
            "analysis: {}\n"
            "normalization: {}\n"
            "voice_planner: {}\n"
        ),
        encoding="utf-8",
    )
    (root / "storyforge" / "defaults").mkdir(parents=True, exist_ok=True)
    (root / "storyforge" / "__init__.py").write_text('__version__ = "0.1.0a1"\n', encoding="utf-8")
    (root / "storyforge" / "cli.py").write_text("def main():\n    return 0\n", encoding="utf-8")
    (root / "storyforge" / "release_check.py").write_text(
        "def main(argv=None):\n    return 0\n", encoding="utf-8"
    )
    (root / "storyforge" / "defaults" / "config.yaml").write_text("paths: {}\n", encoding="utf-8")
    (root / ".github" / "workflows").mkdir(parents=True, exist_ok=True)
    (root / ".github" / "workflows" / "ci.yml").write_text("name: ci\n", encoding="utf-8")
    if include_readme:
        (root / "README.md").write_text(
            "# StoryForge\n\nStoryForge is under active development.\n", encoding="utf-8"
        )


def test_public_packages_import() -> None:
    assert app.__name__ == "app"
    assert storyforge.__version__ == "0.1.0a1"


def test_pyproject_metadata_parses_and_version_matches() -> None:
    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    project = data["project"]
    assert project["name"] == "storyforge"
    assert project["version"] == storyforge.__version__
    assert project["readme"] == "README.md"
    assert project["license"]["file"] == "LICENSE"
    assert project["requires-python"] == ">=3.11"
    assert project["scripts"]["storyforge"] == "storyforge.cli:main"


def test_release_check_passes_on_repo() -> None:
    findings = collect_findings(REPO_ROOT)
    errors = [finding for finding in findings if finding.severity == "error"]
    assert errors == []


def test_release_check_fails_on_missing_required_file(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    _init_git_repo(root)
    _write_minimal_release_files(root, include_readme=False)
    subprocess.run(["git", "add", "."], cwd=root, check=True, capture_output=True, text=True)
    findings = collect_findings(root)
    assert any(f.code == "missing-required-file" and f.path == "README.md" for f in findings)


def test_release_check_fails_on_absolute_path(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    _init_git_repo(root)
    _write_minimal_release_files(root)
    docs = root / "docs"
    docs.mkdir(parents=True, exist_ok=True)
    (docs / "note.md").write_text(
        f"Use {Path.home() / 'private' / 'path'} for local testing.\n", encoding="utf-8"
    )
    subprocess.run(["git", "add", "."], cwd=root, check=True, capture_output=True, text=True)
    findings = collect_findings(root)
    assert any(f.code == "local_absolute_path" for f in findings)


def test_release_check_fails_on_tracked_generated_audio(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    _init_git_repo(root)
    _write_minimal_release_files(root)
    (root / "sample.wav").write_bytes(b"RIFF0000WAVE")
    subprocess.run(["git", "add", "."], cwd=root, check=True, capture_output=True, text=True)
    findings = collect_findings(root)
    assert any(f.code == "forbidden-tracked-artifact" and f.path == "sample.wav" for f in findings)


def test_cli_help_and_version() -> None:
    help_run = _run_storyforge("--help")
    version_run = _run_storyforge("--version")
    assert help_run.returncode == 0
    assert "Usage: storyforge" in help_run.stdout
    assert version_run.returncode == 0
    assert storyforge.__version__ in version_run.stdout
    assert help_run.stderr == ""
    assert version_run.stderr == ""


def test_cli_invalid_command_nonzero() -> None:
    result = _run_storyforge("not-a-command")
    assert result.returncode == 2
    assert "Unknown command" in result.stderr


def test_validate_command_passes_on_repo() -> None:
    result = _run_storyforge("validate", "--root", str(REPO_ROOT))
    assert result.returncode == 0
    assert "release audit passed" in result.stdout.lower()


def test_environment_overrides_are_applied(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("STORYFORGE_BOOKS_DIR", str(tmp_path / "books"))
    monkeypatch.setenv("STORYFORGE_OUTPUT_DIR", str(tmp_path / "output"))
    monkeypatch.setenv("KOKORO_API_URL", "http://example.invalid:8880/v1")
    monkeypatch.setenv("KOKORO_VOICE", "test_voice")
    monkeypatch.setenv("KOKORO_SPEED", "1.25")
    monkeypatch.setenv("STORYFORGE_CHUNK_CHARS", "4321")
    settings = load_settings()
    assert settings.paths.books_dir == tmp_path / "books"
    assert settings.paths.output_dir == tmp_path / "output"
    assert settings.kokoro.api_url == "http://example.invalid:8880/v1"
    assert settings.kokoro.voice == "test_voice"
    assert settings.kokoro.speed == 1.25
    assert settings.conversion.chunk_chars == 4321
