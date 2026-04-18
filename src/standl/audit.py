"""Ok/warn/fail audit records + markdown renderer.

The audit module is the only stable output contract of ``modes.validate`` and
``modes.meta_check``. Downstream tools read ``audit.md`` severity to decide
whether to proceed, so the record shape and severity semantics are fixed:

- ``ok``   — check ran and found no issue.
- ``warn`` — check ran and found something worth flagging, but not fatal.
             Examples: perfect confound of condition / batch, low-confidence
             extraction, missing-but-optional fields.
- ``fail`` — check ran and found something the downstream pipeline cannot
             safely ignore. Examples: a sample file missing from disk, a
             contrast referencing an undeclared factor.

Checks are free to emit multiple records. One record per offense is usually
clearer than one aggregated record, because ``audit.md`` becomes
grep/sed-friendly. When nothing is wrong, a check still emits one ``ok``
summary record so the rendered report confirms the check actually ran.
"""
from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Iterable

from pydantic import BaseModel, Field


class Severity(str, Enum):
    OK = "ok"
    WARN = "warn"
    FAIL = "fail"


# Severity ordering used by ``worst_severity``. Higher = worse.
_RANK = {Severity.OK: 0, Severity.WARN: 1, Severity.FAIL: 2}


class AuditRecord(BaseModel):
    check: str
    status: Severity
    message: str
    evidence: dict[str, Any] | None = None


class AuditReport(BaseModel):
    dataset_id: str
    records: list[AuditRecord] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    def add(self, record: AuditRecord) -> None:
        self.records.append(record)

    def by_check(self, check: str) -> list[AuditRecord]:
        return [r for r in self.records if r.check == check]

    def by_status(self, status: Severity | str) -> list[AuditRecord]:
        s = Severity(status) if not isinstance(status, Severity) else status
        return [r for r in self.records if r.status == s]

    def worst_severity(self) -> Severity:
        if not self.records:
            return Severity.OK
        return max(self.records, key=lambda r: _RANK[r.status]).status


# -------- markdown rendering --------

_BADGE = {Severity.OK: "OK", Severity.WARN: "WARN", Severity.FAIL: "FAIL"}


def _counts(records: Iterable[AuditRecord]) -> dict[str, int]:
    c = {"ok": 0, "warn": 0, "fail": 0}
    for r in records:
        c[r.status.value] += 1
    return c


def render_markdown(report: AuditReport) -> str:
    """Group records by check name; sort checks worst-first so the top of the
    file is where the reader's eye lands on problems.
    """
    by_check: dict[str, list[AuditRecord]] = {}
    for r in report.records:
        by_check.setdefault(r.check, []).append(r)

    def _check_rank(name: str) -> int:
        return max(_RANK[r.status] for r in by_check[name])

    ordered_checks = sorted(by_check, key=lambda n: (-_check_rank(n), n))

    counts = _counts(report.records)
    worst = report.worst_severity().value
    lines: list[str] = [
        f"# Audit: {report.dataset_id}",
        "",
        f"Worst severity: **{worst}** | "
        f"Records: {len(report.records)} "
        f"(ok={counts['ok']} warn={counts['warn']} fail={counts['fail']})",
        f"Generated: {report.created_at.isoformat()}",
        "",
        "## Results",
    ]

    for check in ordered_checks:
        check_worst = max(_RANK[r.status] for r in by_check[check])
        header_badge = [k for k, v in _RANK.items() if v == check_worst][0].value.upper()
        lines += ["", f"### {header_badge} — {check}"]
        for r in by_check[check]:
            prefix = f"- **{_BADGE[r.status]}** {r.message}"
            lines.append(prefix)
            if r.evidence:
                for k, v in r.evidence.items():
                    lines.append(f"    - `{k}`: {v}")

    lines.append("")
    return "\n".join(lines)
