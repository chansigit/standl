"""standl CLI.

Three entry modes (see ``modes.py`` for semantics):

    standl run <source> -o <dir>          # download + extract + validate
    standl validate <dir> [--h5ad X]      # data already local; reconcile
    standl meta-check <dir> [--paper URL] # only cross-check metadata claims

``<source>`` can be a DOI, a paper URL (PMC/bioRxiv/publisher), or a
repository accession (GSE/GSM, E-MTAB, CxG collection id, ...). The core
never branches on the format; extractors self-rate via ``can_handle``.

Exit code: ``0`` when worst audit severity is ok/warn, ``1`` when fail —
so CI can gate on ``standl validate <dir>`` directly.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

from . import modes
from .audit import AuditReport, Severity
from .schema import Source


def _source_from_string(s: str) -> Source:
    """Minimal parsing. Extractors' can_handle does the real dispatch."""
    src = Source()
    if s.startswith(("http://", "https://")):
        src.paper_url = s
    elif s.startswith("10."):
        src.paper_doi = s
    else:
        src.accessions = [s]
    return src


def _count_by_severity(report: AuditReport) -> dict[str, int]:
    c = {"ok": 0, "warn": 0, "fail": 0}
    for r in report.records:
        c[r.status.value] += 1
    return c


def _print_summary(
    dataset_dir: Path,
    report: AuditReport,
    mode: str,
    extra_hints: list[str] | None = None,
) -> None:
    c = _count_by_severity(report)
    worst = report.worst_severity().value
    print(
        f"[standl {mode}] {dataset_dir}/audit.md — worst severity: {worst} "
        f"(ok={c['ok']} warn={c['warn']} fail={c['fail']})",
        file=sys.stderr,
    )
    for hint in extra_hints or []:
        print(f"[standl {mode}] hint: {hint}", file=sys.stderr)


def _design_needs_hand_edit(dataset_dir: Path) -> bool:
    """A fresh ``standl run`` produces a design with only deterministic facts —
    no ``factors`` / ``contrasts`` / ``condition``. That's by design (the
    skill is the handoff point), but users shouldn't have to intuit it.
    """
    try:
        d = yaml.safe_load((dataset_dir / "design.yaml").read_text())
    except Exception:
        return False
    if d.get("factors") or d.get("contrasts"):
        return False
    for s in d.get("samples", []):
        if s.get("condition"):
            return False
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="standl")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_run = sub.add_parser("run", help="Full: extract + fetch + validate")
    p_run.add_argument("source", help="DOI, URL, or accession (e.g. GSE123456)")
    p_run.add_argument("-o", "--out", required=True, type=Path)
    p_run.add_argument("--refresh", action="store_true",
                       help="invalidate cached SOFT / API responses under <out>/paper/ before extracting")

    p_val = sub.add_parser("validate", help="Reconcile local data with design.yaml")
    p_val.add_argument("dataset_dir", type=Path)
    p_val.add_argument("--h5ad", type=Path, default=None)
    p_val.add_argument(
        "--deep", action="store_true",
        help="verify every file's sha256 against the manifest (not just size)",
    )

    p_meta = sub.add_parser("meta-check", help="Cross-check metadata claims only")
    p_meta.add_argument("dataset_dir", type=Path)
    p_meta.add_argument("--paper", default=None, help="Paper DOI/URL to re-extract from")
    p_meta.add_argument("--h5ad", type=Path, default=None,
                        help="Processed h5ad to cross-check against design")
    p_meta.add_argument("--write-design", action="store_true",
                        help="Overwrite design.yaml + write provenance.json (default: read-only)")
    p_meta.add_argument("--refresh", action="store_true",
                        help="invalidate cached SOFT / API responses under <dataset_dir>/ before re-extracting")

    args = parser.parse_args(argv)

    if args.cmd == "run":
        report = modes.run(_source_from_string(args.source), args.out, refresh=args.refresh)
        hints: list[str] = []
        if _design_needs_hand_edit(args.out):
            hints.append(
                "design.yaml has no factors / contrasts / condition — "
                "hand-edit per skills/standl/SKILL.md, then `standl validate`"
            )
        _print_summary(args.out, report, mode="run", extra_hints=hints)
    elif args.cmd == "validate":
        report = modes.validate(args.dataset_dir, h5ad=args.h5ad, deep=args.deep)
        _print_summary(args.dataset_dir, report, mode="validate")
    elif args.cmd == "meta-check":
        paper_src = _source_from_string(args.paper) if args.paper else None
        report = modes.meta_check(
            args.dataset_dir,
            paper_source=paper_src,
            h5ad=args.h5ad,
            write_design=args.write_design,
            refresh=args.refresh,
        )
        _print_summary(args.dataset_dir, report, mode="meta-check")
    else:
        parser.error(f"unknown command {args.cmd}")

    return 1 if report.worst_severity() == Severity.FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
