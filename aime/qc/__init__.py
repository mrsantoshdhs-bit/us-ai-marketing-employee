"""Automated quality control for LinkedIn posts, run before a post can be scheduled.

Each check returns pass, fail or uncertain:
- any fail       -> decision "fail": the writer tries another draft; if none passes, the post is not created
- any uncertain  -> decision "hold": the post is kept for a person to review (it is never auto-published)
- all pass       -> decision "pass"
The report (with the content hash it was computed on) is stored in content_items.quality["qc"].
"""

from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass

from aime.timeutil import utcnow

QC_VERSION = 1


@dataclass
class CheckResult:
    name: str
    status: str  # pass | fail | uncertain
    detail: str = ""


def content_sha256(title: str, body: str, hashtags: list[str]) -> str:
    return hashlib.sha256("\n".join([title, body, " ".join(hashtags or [])]).encode("utf-8")).hexdigest()


def decide(results: list[CheckResult]) -> str:
    if any(r.status == "fail" for r in results):
        return "fail"
    if any(r.status == "uncertain" for r in results):
        return "hold"
    return "pass"


def build_report(results: list[CheckResult], title: str, body: str, hashtags: list[str]) -> dict:
    decision = decide(results)
    return {"version": QC_VERSION, "status": {"pass": "passed", "hold": "held", "fail": "failed"}[decision],
            "decision": decision, "checked_at": utcnow().isoformat(timespec="seconds"),
            "content_sha256": content_sha256(title, body, hashtags), "checks": [asdict(r) for r in results]}


def summary(report: dict | None) -> str:
    """One line for lists and alerts: the failing or uncertain checks, or 'all checks passed'."""
    if not report:
        return "not checked yet"
    issues = [f"{c['name']}: {c['detail']}" for c in report.get("checks", []) if c["status"] != "pass"]
    return "; ".join(issues) if issues else "all checks passed"
