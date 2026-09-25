#!/usr/bin/env python
"""Scan the repository for committed secrets and for secret-bearing files that should be git-ignored.

Usage:
    python scripts/secret_scan.py              scan git-tracked files (falls back to walking the folder outside git)
    python scripts/secret_scan.py --include-tests
    python scripts/secret_scan.py --root <dir>

Exit code 0 = nothing found, 1 = findings (printed with file and line, never the secret value itself).
Test fixtures under tests/ use obviously fake values and are skipped unless --include-tests is given.
"""

import argparse
import re
import subprocess
import sys
from pathlib import Path

PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("google_client_secret", re.compile(r"GOCSPX-[A-Za-z0-9_\-]{10,}")),
    ("google_access_token", re.compile(r"\bya29\.[A-Za-z0-9_\-]{20,}")),
    ("google_refresh_token", re.compile(r"\b1//[A-Za-z0-9_\-]{30,}")),
    ("google_api_key", re.compile(r"\bAIza[A-Za-z0-9_\-]{30,}")),
    ("openai_style_key", re.compile(r"\bsk-[A-Za-z0-9_\-]{30,}")),
    ("github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}")),
    ("telegram_bot_token", re.compile(r"\b\d{8,12}:[A-Za-z0-9_\-]{35}\b")),
    ("aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("private_key_block", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----")),
    ("hardcoded_credential", re.compile(r"""(?i)\b(?:password|passwd|secret|api[_-]?key|access[_-]?token|refresh[_-]?token|client[_-]?secret)\b\s*[:=]\s*['"](?!\s*['"])(?!.*(?:example|placeholder|changeme|your[_-]|xxx|<|\{|\$|None|test|dummy|fake|redacted))(?=[^'"\s]*(?:[0-9]|(?-i:[A-Z])))[^'"\s]{12,}['"]""")),
    ("database_url_with_password", re.compile(r"(?i)\b(?:postgres(?:ql)?(?:\+\w+)?|mysql)://[^:/\s]+:(?!jarvis@|password@|user@|\*+|<)[^@/\s]{6,}@(?!localhost|127\.0\.0\.1)")),
]
FORBIDDEN_TRACKED = re.compile(r"(^|/)(\.env(\.(?!example$).*)?|token\.json|credentials\.json|client_secret[^/]*\.json|.*\.pem|.*\.key|\.jarvis/.*|telegram_token.*)$")
REQUIRED_IGNORES = [".env", ".jarvis/", "token.json", "credentials.json", "client_secret*.json", "logs/", "*.log", "models/"]
SKIP_SUFFIXES = {".png", ".jpg", ".ico", ".onnx", ".pdf", ".pyc", ".lock", ".zip", ".bin", ".db", ".sqlite3"}
MAX_BYTES = 1_000_000


def tracked_files(root: Path) -> list[Path]:
    try:
        out = subprocess.run(["git", "ls-files", "-z"], cwd=root, capture_output=True, check=True, timeout=30).stdout
        return [root / p for p in out.decode("utf-8", "replace").split("\0") if p]
    except (OSError, subprocess.SubprocessError):
        skip = {".git", ".venv", "node_modules", "__pycache__", "models", "logs", ".jarvis", "scratch"}
        return [p for p in root.rglob("*") if p.is_file() and not (set(p.relative_to(root).parts) & skip)]


def scan(root: Path, include_tests: bool = False) -> list[str]:
    findings: list[str] = []
    for path in tracked_files(root):
        rel = path.relative_to(root).as_posix()
        if FORBIDDEN_TRACKED.search(rel):
            findings.append(f"{rel}: a secret-bearing file is tracked (it must be git-ignored)")
            continue
        if path.suffix.lower() in SKIP_SUFFIXES or (rel.startswith("tests/") and not include_tests) or rel == "scripts/secret_scan.py":
            continue
        try:
            if path.stat().st_size > MAX_BYTES:
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for number, line in enumerate(text.splitlines(), start=1):
            if "secret-scan: allow" in line:
                continue
            for name, pattern in PATTERNS:
                if pattern.search(line):
                    findings.append(f"{rel}:{number}: possible {name}")
    gitignore = root / ".gitignore"
    ignored = gitignore.read_text(encoding="utf-8").splitlines() if gitignore.exists() else []
    for needed in REQUIRED_IGNORES:
        if needed not in (line.strip() for line in ignored) and needed.rstrip("/") not in (line.strip().rstrip("/").lstrip("/") for line in ignored):
            findings.append(f".gitignore: missing entry '{needed}'")
    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--include-tests", action="store_true")
    args = parser.parse_args(argv)
    findings = scan(args.root, args.include_tests)
    for finding in findings:
        print(finding)
    print(f"secret scan: {'FAILED, ' + str(len(findings)) + ' finding(s)' if findings else 'clean'}")
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
