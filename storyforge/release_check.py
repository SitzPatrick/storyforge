from __future__ import annotations

import argparse
import re
import subprocess
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover
    import tomli as tomllib  # type: ignore

REQUIRED_FILES = [
    "README.md",
    "LICENSE",
    "CHANGELOG.md",
    "CONTRIBUTING.md",
    "SECURITY.md",
    "CODE_OF_CONDUCT.md",
    "pyproject.toml",
    ".gitignore",
    "config/config.yaml",
    "storyforge/__init__.py",
    "storyforge/cli.py",
    "storyforge/release_check.py",
    "storyforge/defaults/config.yaml",
    ".github/workflows/ci.yml",
]

README_WARNING = "StoryForge is under active development"
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+(?:[abc]\d+|rc\d+)?$")
LOCAL_ABSOLUTE_PATH_PARTS = ("/" "Users/", "/" "home/", "C:\\" "Users")

SECRET_PATTERNS = {
    "private_key": re.compile(r"-----BEGIN (?:RSA|OPENSSH|EC|DSA) PRIVATE KEY-----"),
    "bearer_token": re.compile(r"\bBearer\s+[A-Za-z0-9._\-]+"),
    "token_query": re.compile(
        r"[?&](?:token|access_token|api[_-]?key|secret)=[^\s&]+", re.IGNORECASE
    ),
    "local_absolute_path": re.compile(
        "|".join(re.escape(part) for part in LOCAL_ABSOLUTE_PATH_PARTS)
    ),
    "private_ip": re.compile(
        r"\b(?:10|192\.168)\.\d{1,3}\.\d{1,3}\b|\b172\.(?:1[6-9]|2\d|3[0-1])\.\d{1,3}\.\d{1,3}\b"
    ),
    "internal_hostname": re.compile(r"(?<!http://)(?<!https://)Kokoro-FastAPI", re.IGNORECASE),
}
FORBIDDEN_TRACKED_EXTENSIONS = {".wav", ".m4b", ".mp3", ".aac", ".flac", ".ogg", ".mp4", ".mov"}
FORBIDDEN_TRACKED_PATHS = {
    ".env",
    ".env.local",
    ".env.production",
    ".env.development",
    ".python-version",
    ".DS_Store",
}


@dataclass(frozen=True)
class Finding:
    severity: str
    code: str
    path: str
    message: str


def collect_findings(root: Path) -> list[Finding]:
    root = root.resolve()
    findings: list[Finding] = []
    findings.extend(_check_required_files(root))
    findings.extend(_check_pyproject(root))
    findings.extend(_check_tracked_files(root))
    findings.extend(_check_markdown_links(root))
    findings.extend(_scan_tracked_text(root))
    return findings


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Audit a StoryForge repository for public-release readiness."
    )
    parser.add_argument(
        "--root", default=str(Path(__file__).resolve().parents[1]), help="Repository root to audit"
    )
    args = parser.parse_args(list(argv) if argv is not None else None)
    findings = collect_findings(Path(args.root))
    errors = [finding for finding in findings if finding.severity == "error"]
    warnings = [finding for finding in findings if finding.severity == "warning"]
    if findings:
        for finding in findings:
            print(f"{finding.severity.upper()}: {finding.code}: {finding.path}: {finding.message}")
    else:
        print("StoryForge release audit passed")
    if warnings and not errors:
        print(f"Warnings: {len(warnings)}")
    return 0 if not errors else 1


def _check_required_files(root: Path) -> list[Finding]:
    findings: list[Finding] = []
    for rel in REQUIRED_FILES:
        if not (root / rel).exists():
            findings.append(
                Finding("error", "missing-required-file", rel, "required public file is missing")
            )
    return findings


def _check_pyproject(root: Path) -> list[Finding]:
    path = root / "pyproject.toml"
    if not path.exists():
        return []
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    project = data.get("project") or {}
    findings: list[Finding] = []
    version = str(project.get("version", "")).strip()
    if not version:
        findings.append(
            Finding("error", "missing-version", "pyproject.toml", "project.version is missing")
        )
    elif not VERSION_RE.match(version):
        findings.append(
            Finding(
                "error",
                "invalid-version",
                "pyproject.toml",
                f"project.version is not a supported pre-release version: {version}",
            )
        )
    if project.get("readme") != "README.md":
        findings.append(
            Finding(
                "error",
                "readme-reference",
                "pyproject.toml",
                "project.readme should reference README.md",
            )
        )
    license_value = project.get("license") or {}
    if not isinstance(license_value, dict) or license_value.get("file") != "LICENSE":
        findings.append(
            Finding(
                "error",
                "license-reference",
                "pyproject.toml",
                "project.license should reference LICENSE",
            )
        )
    if project.get("requires-python") != ">=3.11":
        findings.append(
            Finding(
                "warning",
                "python-version",
                "pyproject.toml",
                "project.requires-python should target Python 3.11+",
            )
        )
    scripts = project.get("scripts") or {}
    if "storyforge" not in scripts:
        findings.append(
            Finding(
                "warning",
                "cli-entrypoint",
                "pyproject.toml",
                "storyforge console script is missing",
            )
        )
    return findings


def _tracked_files(root: Path) -> list[str]:
    proc = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z"], check=True, capture_output=True, text=True
    )
    return [item for item in proc.stdout.split("\0") if item]


def _check_tracked_files(root: Path) -> list[Finding]:
    findings: list[Finding] = []
    try:
        tracked = _tracked_files(root)
    except Exception as exc:  # noqa: BLE001
        return [
            Finding("warning", "git-unavailable", ".", f"could not inspect tracked files: {exc}")
        ]
    for rel in tracked:
        rel_path = Path(rel)
        if (
            rel_path.suffix.lower() in FORBIDDEN_TRACKED_EXTENSIONS
            or rel_path.name in FORBIDDEN_TRACKED_PATHS
        ):
            findings.append(
                Finding(
                    "error",
                    "forbidden-tracked-artifact",
                    rel,
                    "generated audio or local environment artifact is tracked",
                )
            )
        if rel_path.suffix.lower() == ".env" and rel_path.name != ".env.example":
            findings.append(
                Finding(
                    "error",
                    "forbidden-env-file",
                    rel,
                    "tracked environment file is not public-safe",
                )
            )
    return findings


def _check_markdown_links(root: Path) -> list[Finding]:
    findings: list[Finding] = []
    for rel in _candidate_markdown_files(root):
        path = root / rel
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        for link in _extract_links(text):
            if _is_external_link(link):
                continue
            target = (path.parent / link.split("#", 1)[0]).resolve()
            if not target.exists():
                findings.append(
                    Finding(
                        "warning", "broken-link", rel, f"markdown link target is missing: {link}"
                    )
                )
    return findings


def _scan_tracked_text(root: Path) -> list[Finding]:
    findings: list[Finding] = []
    try:
        tracked = _tracked_files(root)
    except Exception:
        return findings
    skipped_paths = {
        Path("storyforge/release_check.py"),
        Path("tests/test_release_readiness.py"),
    }
    for rel in tracked:
        path = root / rel
        rel_path = Path(rel)
        if rel_path in skipped_paths or rel_path.parts[:1] == ("tests",):
            continue
        if not path.is_file():
            continue
        if not _is_probably_text_file(path):
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        for code, pattern in SECRET_PATTERNS.items():
            if pattern.search(text):
                findings.append(
                    Finding(
                        "error", code, rel, "tracked file contains a potentially sensitive pattern"
                    )
                )
    return findings


def _candidate_markdown_files(root: Path) -> list[str]:
    files = ["README.md"]
    docs = root / "docs"
    if docs.exists():
        for path in sorted(docs.rglob("*.md")):
            files.append(str(path.relative_to(root)))
    return files


def _extract_links(text: str) -> list[str]:
    return re.findall(r"\[[^\]]+\]\(([^)]+)\)", text)


def _is_external_link(link: str) -> bool:
    parsed = urlparse(link)
    return bool(parsed.scheme and parsed.scheme != "file") or link.startswith("#")


def _is_probably_text_file(path: Path) -> bool:
    if path.suffix.lower() in {
        ".json",
        ".yaml",
        ".yml",
        ".toml",
        ".md",
        ".txt",
        ".py",
        ".sh",
        ".ini",
        ".cfg",
        ".csv",
    }:
        return True
    return path.name in {
        "LICENSE",
        "CHANGELOG.md",
        "CONTRIBUTING.md",
        "SECURITY.md",
        "CODE_OF_CONDUCT.md",
    }


if __name__ == "__main__":
    raise SystemExit(main())
