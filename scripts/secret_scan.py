"""Block secrets, personal data, and runtime data from being committed.

Usage:
  python scripts/secret_scan.py --staged   # what is about to be committed (used by the pre-commit hook)
  python scripts/secret_scan.py --all      # every tracked or committable file
Exit code 1 when anything is found.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys

FORBIDDEN_PATHS = [
    (re.compile(r"(^|/)\.env$|(^|/)\.env\.(?!example$)"), "environment file"),
    (re.compile(r"(^|/)(data|demo_data|backups)/"), "runtime data folder (CRM, leads, logs)"),
    (re.compile(r"\.(db|db-wal|db-shm|sqlite3?|log|jsonl)$"), "database or log file"),
    (re.compile(r"\.(pem|key|p12|pfx)$|(^|/)id_rsa"), "private key"),
    (re.compile(r"(^|/)\.claude/|(^|/)\.venv/"), "local tool folder"),
]
SECRET_PATTERNS = [
    (re.compile(r"AKIA[0-9A-Z]{16}"), "AWS access key"),
    (re.compile(r"gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{20,}"), "GitHub token"),
    (re.compile(r"xox[baprs]-[A-Za-z0-9-]{10,}"), "Slack token"),
    (re.compile(r"AIza[0-9A-Za-z_-]{35}"), "Google API key"),
    (re.compile(r"sk-(?:ant-)?[A-Za-z0-9_-]{20,}"), "API secret key"),
    (re.compile(r"BEGIN [A-Z ]*PRIVATE KEY"), "private key block"),
    (re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}"), "JWT"),
    (re.compile(r"\bAQ[A-Za-z0-9_-]{60,}"), "LinkedIn access token"),
    (re.compile(r"^(?:LINKEDIN_CLIENT_SECRET|SMTP_PASSWORD|CENSUS_API_KEY|INBOUND_API_TOKEN)=\S+", re.M),
     "credential value in env-style file"),
    (re.compile(r"(?i)(?:password|client_secret|api_key)\s*=\s*[\"'][^\"'{}<>\s]{8,}[\"']"), "hard-coded credential"),
    (re.compile(r"[A-Za-z]:[\\/]+Users[\\/]+(?!Public)[A-Za-z0-9._-]+[\\/]"), "local Windows user path"),
    (re.compile(r"[A-Za-z0-9._%+-]+@(?:gmail|googlemail|yahoo|hotmail|outlook|live|icloud|aol|proton(?:mail)?)\.[a-z.]+",
                re.I), "personal email address"),
]
# Literal test fixtures that intentionally look sensitive.
ALLOWED_SNIPPETS = {"x@gmail.com"}


def _git(*args: str) -> str:
    return subprocess.run(["git", *args], capture_output=True, text=True, check=True, encoding="utf-8",
                          errors="replace").stdout


def _files(staged: bool) -> list[str]:
    if staged:
        return [f for f in _git("diff", "--cached", "--name-only", "--diff-filter=ACMR").splitlines() if f]
    tracked = _git("ls-files").splitlines()
    untracked = _git("ls-files", "--others", "--exclude-standard").splitlines()
    return sorted(set(tracked) | set(untracked))


def _content(path: str, staged: bool) -> str | None:
    try:
        if staged:
            return _git("show", f":{path}")
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read()
    except (OSError, subprocess.CalledProcessError):
        return None


def scan(staged: bool) -> list[str]:
    findings: list[str] = []
    for path in _files(staged):
        norm = path.replace("\\", "/")
        if norm == "scripts/secret_scan.py":
            continue  # contains the patterns themselves
        for pattern, label in FORBIDDEN_PATHS:
            if pattern.search(norm):
                findings.append(f"{path}: {label} must not be committed")
        text = _content(path, staged)
        if text is None or "\x00" in text[:2000]:
            continue  # binary (images) or unreadable
        for pattern, label in SECRET_PATTERNS:
            for m in pattern.finditer(text):
                if m.group(0) in ALLOWED_SNIPPETS:
                    continue
                line = text.count("\n", 0, m.start()) + 1
                findings.append(f"{path}:{line}: possible {label}")
    return findings


def main() -> int:
    ap = argparse.ArgumentParser()
    group = ap.add_mutually_exclusive_group()
    group.add_argument("--staged", action="store_true")
    group.add_argument("--all", action="store_true")
    args = ap.parse_args()
    findings = scan(staged=not args.all)
    for f in findings:
        print("BLOCKED:", f)
    if findings:
        print(f"\nsecret_scan: {len(findings)} finding(s). Remove them (or unstage) before committing.")
        return 1
    print("secret_scan: clean")
    return 0


if __name__ == "__main__":
    sys.exit(main())
