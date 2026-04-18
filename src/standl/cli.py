"""standl CLI.

Three entry modes (see ``modes.py`` for semantics):

    standl run <source> -o <dir>          # download + extract + validate
    standl validate <dir> [--h5ad X]      # data already local; reconcile
    standl meta-check <dir> [--paper URL] # only cross-check metadata claims

``<source>`` can be a DOI, a paper URL (PMC/bioRxiv/publisher), or a
repository accession (GSE/GSM, E-MTAB, CxG collection id, ...). The core
never branches on the format; extractors self-rate via ``can_handle``.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import modes
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="standl")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_run = sub.add_parser("run", help="Full: extract + fetch + validate")
    p_run.add_argument("source", help="DOI, URL, or accession (e.g. GSE123456)")
    p_run.add_argument("-o", "--out", required=True, type=Path)

    p_val = sub.add_parser("validate", help="Reconcile local data with design.yaml")
    p_val.add_argument("dataset_dir", type=Path)
    p_val.add_argument("--h5ad", type=Path, default=None)

    p_meta = sub.add_parser("meta-check", help="Cross-check metadata claims only")
    p_meta.add_argument("dataset_dir", type=Path)
    p_meta.add_argument("--paper", default=None, help="Paper DOI/URL to re-extract from")
    p_meta.add_argument("--h5ad", type=Path, default=None,
                        help="Processed h5ad to cross-check against design")
    p_meta.add_argument("--write-design", action="store_true",
                        help="Overwrite design.yaml + write provenance.json (default: read-only)")

    args = parser.parse_args(argv)

    if args.cmd == "run":
        modes.run(_source_from_string(args.source), args.out)
    elif args.cmd == "validate":
        modes.validate(args.dataset_dir, h5ad=args.h5ad)
    elif args.cmd == "meta-check":
        paper_src = _source_from_string(args.paper) if args.paper else None
        modes.meta_check(
            args.dataset_dir,
            paper_source=paper_src,
            h5ad=args.h5ad,
            write_design=args.write_design,
        )
    else:
        parser.error(f"unknown command {args.cmd}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
