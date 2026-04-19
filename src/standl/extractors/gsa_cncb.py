"""gsa-cncb — Genome Sequence Archive at CNCB / NGDC (Beijing).

GSA is the Chinese counterpart to NCBI SRA / EBI ENA. Three accession
families live under the NGDC roof:

- ``CRA`` — open-access raw sequencing archive. Files are fastq.gz
  (one or two per run), listed as ``CRA/<SAMC>/<CRX>/<CRR>/CRD<n>.gz``
  under ``https://download.cncb.ac.cn/gsa/``.
- ``HRA`` — human raw sequencing, **controlled access**. Downloads
  require a DAC (Data Access Committee) application.
- ``OMIX`` — a separate NGDC database for omics deposits (processed
  matrices, h5ads, etc.). Different URL structure. Not handled here —
  left to a future extractor.

Like BioStudies, GSA at the CRA level holds raw fastq only. Per
``docs/roadmap.md`` ("SRA / fastq-level downloads deferred"), this
extractor:

- Scrapes the browse page (open, no auth) for title / organism / per-
  experiment sample list, lands them in ``Sample.extra``.
- Emits a ``data_format`` failure so ``modes.run``'s
  ``extractor_partial_failure`` surfaces it as FAIL — same contract
  geo-soft uses for pooled-series deposits. Users after analysis-ready
  counts should look at the paper's companion Figshare / Zenodo /
  Science Data Bank deposit, or apply to the DAC.
- HRA triggers a distinct ``data_access`` failure (need DAC approval,
  won't be auto-downloaded).
- OMIX is out of scope for first ship.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from ..schema import PartialDesign, PartialSample, Source
from .base import make_pv, register

_pv = make_pv("gsa-cncb", default_confidence=0.9)

_ACCESSION_RE = re.compile(r"^(CRA|HRA|OMIX|PRJCA)[0-9]+$")
_BROWSE_URL_RE = re.compile(
    r"ngdc\.cncb\.ac\.cn/(?:gsa|gsa-human|omix)/browse/([A-Z]+[0-9]+)",
    re.IGNORECASE,
)

BROWSE_BASE_CRA = "https://ngdc.cncb.ac.cn/gsa/browse"
BROWSE_BASE_HRA = "https://ngdc.cncb.ac.cn/gsa-human/browse"

# taxon id → canonical species label. Keep the list short; ``organism``
# stays None for anything not listed — we don't fabricate from numeric ids.
_TAXON_MAP = {
    "9606": "Homo sapiens",
    "10090": "Mus musculus",
    "10116": "Rattus norvegicus",
    "7227": "Drosophila melanogaster",
    "6239": "Caenorhabditis elegans",
    "4932": "Saccharomyces cerevisiae",
    "7955": "Danio rerio",
    "9544": "Macaca mulatta",
    "9913": "Bos taurus",
    "9823": "Sus scrofa",
}

_EXP_ROW_RE = re.compile(
    # <tr class="experiment"> ... <a href="browse/CRAxxxx/CRXyyyyy">CRXyyyyy</a>
    # ... <td>sample_label</td> ... NCBI Taxonomy link ending in ?id=NNNN
    r'class="experiment">'
    r'.*?browse/[A-Z]+[0-9]+/(?P<crx>CRX[0-9]+).*?</a>'
    r'.*?<td>(?P<sample>[^<]+)</td>'
    r'.*?[?&]id=(?P<taxid>\d+)',
    re.DOTALL,
)

# "标题" label followed by optional colon + arbitrary tag/whitespace, then the
# first chunk of plain text. Lenient about closing tags between label and text.
_TITLE_RE = re.compile(
    r'标题[:：]?\s*(?:</?\w+[^>]*>\s*)+([^<\n]{3,300})',
    re.DOTALL,
)


def _classify(accession: str) -> str:
    """Return ``"CRA"`` / ``"HRA"`` / ``"OMIX"`` / ``"PRJCA"`` / ``""``."""
    m = re.match(r"^([A-Z]+)", accession)
    return m.group(1) if m else ""


def _extract_accession(source: Source) -> str | None:
    for acc in source.accessions:
        if _ACCESSION_RE.match(acc):
            return acc
    if source.paper_url:
        m = _BROWSE_URL_RE.search(source.paper_url)
        if m and _ACCESSION_RE.match(m.group(1)):
            return m.group(1)
    return None


def _fetch_browse_html(accession: str, cache_dir: Path | None) -> str:
    """Fetch the NGDC browse page. CRA / HRA live on different paths."""
    if cache_dir is not None:
        cached = cache_dir / f"gsa_cncb_browse_{accession}.html"
        if cached.is_file():
            return cached.read_text(encoding="utf-8", errors="replace")

    import requests

    base = BROWSE_BASE_HRA if accession.startswith("HRA") else BROWSE_BASE_CRA
    r = requests.get(f"{base}/{accession}", timeout=30,
                     headers={"User-Agent": "Mozilla/5.0"})
    r.raise_for_status()
    html = r.text
    if cache_dir is not None:
        cache_dir.mkdir(parents=True, exist_ok=True)
        (cache_dir / f"gsa_cncb_browse_{accession}.html").write_text(
            html, encoding="utf-8",
        )
    return html


def _parse_browse(html: str, accession: str) -> dict[str, Any]:
    """Extract title, organism, and per-experiment rows from the
    browse-page HTML. Returns a best-effort dict — any field may be
    missing if the page structure drifted."""
    out: dict[str, Any] = {"accession": accession, "experiments": []}

    if (m := _TITLE_RE.search(html)):
        out["title"] = m.group(1).strip()

    taxids: list[str] = []
    seen_crx: set[str] = set()
    for m in _EXP_ROW_RE.finditer(html):
        crx = m.group("crx")
        if crx in seen_crx:
            continue
        seen_crx.add(crx)
        sample = m.group("sample").strip()
        taxid = m.group("taxid").strip()
        taxids.append(taxid)
        out["experiments"].append(
            {"crx": crx, "sample_label": sample, "taxon_id": taxid}
        )

    # Majority-vote the organism from taxon ids.
    if taxids:
        from collections import Counter
        top = Counter(taxids).most_common(1)[0][0]
        out["taxon_id"] = top
        if top in _TAXON_MAP:
            out["organism"] = _TAXON_MAP[top]

    return out


class GSACNCBExtractor:
    name = "gsa-cncb"

    def can_handle(self, source: Source) -> float:
        for acc in source.accessions:
            if _ACCESSION_RE.match(acc):
                return 0.9
        if source.paper_url and "ngdc.cncb.ac.cn" in source.paper_url:
            m = _BROWSE_URL_RE.search(source.paper_url)
            if m and _ACCESSION_RE.match(m.group(1)):
                return 0.9
            return 0.4
        return 0.0

    def extract(self, source: Source, cache_dir: Path) -> PartialDesign:
        accession = _extract_accession(source)
        if accession is None:
            return PartialDesign(
                extractor=self.name,
                failures={
                    "accession": (
                        "no CRA/HRA/OMIX/PRJCA accession; pass accessions=['<CRA*|HRA*|OMIX*>'] "
                        "or a https://ngdc.cncb.ac.cn/gsa/browse/<accession> URL"
                    ),
                },
            )

        cache_dir.mkdir(parents=True, exist_ok=True)
        kind = _classify(accession)

        if kind == "OMIX" or kind == "PRJCA":
            return _failure_partial(
                accession, self.name,
                f"OMIX / PRJCA extractor not yet implemented — gsa-cncb currently handles CRA (open) + HRA (controlled). See {accession} at ngdc.cncb.ac.cn manually for now.",
                failure_key="unsupported_accession",
            )

        if kind == "HRA":
            # Don't even try to fetch the page — HRA is DAC-gated at the
            # platform level and the metadata page mostly shows an
            # application form. Return a structured failure with the
            # landing URL so the skill layer can link an application.
            return _failure_partial(
                accession, self.name,
                (f"HRA is controlled access; request via the GSA-Human DAC at "
                 f"https://ngdc.cncb.ac.cn/gsa-human/s/{accession} — "
                 f"standl cannot auto-download this."),
                failure_key="data_access",
                browse_url=f"{BROWSE_BASE_HRA}/{accession}",
            )

        # CRA path: open browse page, scrape metadata, record data_format
        # failure because files are raw fastq.
        try:
            html = _fetch_browse_html(accession, cache_dir)
        except Exception as e:  # noqa: BLE001
            return _failure_partial(
                accession, self.name,
                f"could not fetch browse page: {type(e).__name__}: {e}",
                failure_key="browse_fetch",
            )

        parsed = _parse_browse(html, accession)
        return _build_partial(accession, parsed)


def _failure_partial(
    accession: str,
    extractor_name: str,
    reason: str,
    *,
    failure_key: str,
    browse_url: str | None = None,
) -> PartialDesign:
    sample = PartialSample(sample_id=accession)
    sample.accession = _pv(accession, "gsa-cncb accession", confidence=1.0)
    if browse_url:
        sample.extra["landing_url"] = _pv(browse_url, "gsa-cncb landing page")
    return PartialDesign(
        extractor=extractor_name,
        dataset_id=accession,
        source=Source(
            accessions=[accession],
            repositories=["GSA-CNCB"],
        ),
        samples=[sample],
        failures={failure_key: reason},
    )


def _build_partial(accession: str, parsed: dict[str, Any]) -> PartialDesign:
    organism_pv = None
    if (org := parsed.get("organism")):
        organism_pv = _pv(org, f"taxon id {parsed.get('taxon_id','?')} → species lookup")

    samples: list[PartialSample] = []
    for exp in parsed.get("experiments") or []:
        sid = exp["crx"]
        s = PartialSample(sample_id=sid)
        s.accession = _pv(sid, "gsa-cncb CRX experiment accession", confidence=1.0)
        if organism_pv:
            s.organism = organism_pv
        if exp.get("sample_label"):
            s.extra["sample_label"] = _pv(exp["sample_label"], "browse row td[1]")
        s.extra["parent_study"] = _pv(accession, "gsa-cncb CRA study accession")
        if exp.get("taxon_id"):
            s.extra["taxon_id"] = _pv(exp["taxon_id"], "browse row taxid link")
        samples.append(s)

    if not samples:
        # Couldn't parse experiments — at least emit a study-level PartialSample.
        root = PartialSample(sample_id=accession)
        root.accession = _pv(accession, "gsa-cncb accession", confidence=1.0)
        if organism_pv:
            root.organism = organism_pv
        samples.append(root)

    failures = {
        "data_format": (
            f"GSA CRA archive holds raw fastq only ({len(samples)} experiment(s) "
            "found); per standl's SRA-out-of-scope policy, analysis-ready counts "
            "should come from a companion Figshare / Zenodo / Science Data Bank "
            "deposit if the paper provides one."
        ),
    }

    notes = parsed.get("title")

    return PartialDesign(
        extractor="gsa-cncb",
        dataset_id=accession,
        source=Source(
            accessions=[accession],
            repositories=["GSA-CNCB"],
        ),
        organism=organism_pv,
        samples=samples,
        failures=failures,
        notes=notes,
    )


register(GSACNCBExtractor())
