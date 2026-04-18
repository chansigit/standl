"""GEO SOFT / MINiML / series-matrix extractor.

GEO's format is a moving target — supplementary naming, characteristics keys,
and superseries vs series layout have all drifted. This extractor follows a
**wide-in, narrow-out** policy:

- Try multiple sources in order: SOFT family file → MINiML XML → series matrix
  header → supplementary README. First hit wins per-field. *Only SOFT is
  implemented here; the other fallbacks are hooks for follow-up work.*
- Only emit fields from the canonical schema. Raw oddities go into
  ``Sample.extra`` verbatim — never invent new top-level fields.
- Don't guess condition/batch from characteristics free-text. That is a
  human-in-the-loop step (see ``skills/standl/SKILL.md``). This extractor's
  value is *deterministic, verifiable facts*: accessions, titles,
  characteristics key/value pairs as-given.

When a field is missing (source changed, key renamed), record ``(field, reason)``
in ``PartialDesign.failures`` and move on. Never raise for format drift.
"""
from __future__ import annotations

import gzip
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO

from ..schema import (
    PartialDesign,
    PartialSample,
    ProvenancedValue,
    Source,
)
from .base import make_pv, register

_pv = make_pv("geo-soft", default_confidence=0.95)


_GSE_ACCESSION = re.compile(r"^(GSE|GDS)\d+$")
_GEO_BASE = "https://ftp.ncbi.nlm.nih.gov/geo/series"

# ---------- feature-barcoding + library stem heuristics ----------
#
# Many GEO scRNA studies ship both gene-expression (GEX) and
# feature-barcoding (HASH / MULTI / ADT / HTO / CITE / CellPlex)
# companion libraries as separate ^SAMPLE blocks. GEO treats them as
# peers; downstream tools (stanobj, skill) need to know which is which
# AND which feature-barcoding GSM pairs with which GEX. standl infers
# both heuristically — writes decisions into ``Sample.extra`` with an
# ``evidence`` pointer, never into the canonical slots, so the skill
# layer can override without fighting the schema.

# Title or filename keywords that indicate a feature-barcoding sample.
# Matched case-insensitively as word-ish substrings (token boundary
# enforced by surrounding ``[\W_]`` so "MULTI" doesn't match "multiple").
_FEAT_KEYWORDS = (
    "hash", "hto", "adt", "cite", "cellplex", "feature_barc",
    "multi_tag", "hash_tag", "tagmtx", "hashtag",
)
_FEAT_TITLE_RE = re.compile(
    r"(?:^|[^A-Za-z])(MULTI|HASH|HTO|ADT|CITE)(?:$|[^A-Za-z])",
    re.IGNORECASE,
)

# Canonical per-file suffixes we strip when computing a library stem.
# Order matters — longer first so "hash_tagmtx" wins over "tagmtx".
_FILE_SUFFIXES = (
    "_hash_tagmtx", "_multi_tagmtx", "_tag_mtx", "_tagmtx",
    "_hashtag", "_hto", "_adt", "_cite",
    "_raw_feature_bc_matrix", "_filtered_feature_bc_matrix",
    "_raw_feature", "_filtered_feature",
    "_barcodes", "_features", "_matrix", "_genes",
)
_FILE_EXTENSIONS = (
    ".tsv.gz", ".mtx.gz", ".csv.gz", ".txt.gz",
    ".h5ad", ".h5", ".rds",
    ".tsv", ".mtx", ".csv", ".txt",
)


def _strip_gsm_prefix(name: str) -> str:
    """``GSM8677708_exp1_spleen_l1_barcodes.tsv.gz`` → ``exp1_spleen_l1_barcodes.tsv.gz``."""
    m = re.match(r"^GSM\d+_(.+)$", name)
    return m.group(1) if m else name


def _library_stem(filename: str) -> str | None:
    """Derive a library identifier from a GEO sample-file basename.

    ``GSM8677708_exp1_spleen_l1_barcodes.tsv.gz`` → ``exp1_spleen_l1``
    ``GSM8677712_exp1_spleen_l1_hash_tagmtx.csv.gz`` → ``exp1_spleen_l1``

    Two samples whose first file yields the same stem are assumed to
    belong to the same physical library. Returns ``None`` when the
    filename doesn't carry a recognisable suffix.
    """
    rest = _strip_gsm_prefix(filename.rsplit("/", 1)[-1])
    lower = rest.lower()
    for ext in _FILE_EXTENSIONS:
        if lower.endswith(ext):
            rest = rest[: -len(ext)]
            lower = lower[: -len(ext)]
            break
    for suffix in _FILE_SUFFIXES:
        if lower.endswith(suffix):
            return rest[: -len(suffix)] or None
    # No canonical suffix — bail rather than guess.
    return None


def _looks_like_feature_barcoding(title: str, filenames: list[str]) -> bool:
    """Return True when title / filenames scream "feature barcoding"."""
    if _FEAT_TITLE_RE.search(title or ""):
        return True
    blob = " ".join(filenames).lower()
    return any(k in blob for k in _FEAT_KEYWORDS)


# ---------- SOFT parser ----------

@dataclass
class _Parsed:
    """Raw parse of a SOFT family file. Values stay as lists-of-strings to
    preserve repeated attributes (e.g. multi-line ``Series_summary``)."""
    series: dict[str, list[str]] = field(default_factory=dict)
    platforms: dict[str, dict[str, list[str]]] = field(default_factory=dict)
    samples: dict[str, dict[str, list[str]]] = field(default_factory=dict)


def _open_soft(path: Path) -> IO[str]:
    if path.suffix == ".gz" or str(path).endswith(".soft.gz"):
        return gzip.open(path, "rt", encoding="utf-8", errors="replace")
    return path.open("r", encoding="utf-8", errors="replace")


def _parse_soft(path: Path) -> _Parsed:
    parsed = _Parsed()
    current: tuple[str, str] | None = None  # ("series"|"platform"|"sample"|"ignore", id)

    with _open_soft(path) as fh:
        for raw in fh:
            line = raw.rstrip("\r\n")
            if not line:
                continue
            first = line[0]

            if first == "^":
                kind, _, ident = line[1:].partition("=")
                kind_u = kind.strip().upper()
                ident = ident.strip()
                if kind_u == "SERIES":
                    current = ("series", ident)
                elif kind_u == "PLATFORM":
                    current = ("platform", ident)
                    parsed.platforms.setdefault(ident, {})
                elif kind_u == "SAMPLE":
                    current = ("sample", ident)
                    parsed.samples.setdefault(ident, {})
                else:
                    current = ("ignore", ident)
                continue

            if first == "!":
                if current is None or current[0] == "ignore":
                    continue
                attr, sep, value = line[1:].partition("=")
                if not sep:
                    continue
                attr = attr.strip()
                value = value.strip()
                kind, ident = current
                if kind == "series":
                    parsed.series.setdefault(attr, []).append(value)
                elif kind == "platform":
                    parsed.platforms[ident].setdefault(attr, []).append(value)
                elif kind == "sample":
                    parsed.samples[ident].setdefault(attr, []).append(value)
                continue

            # '#' column descriptors and data-table rows are irrelevant to
            # design extraction; skip.

    return parsed


# ---------- file IO / fetch ----------

def _locate_soft(accession: str, cache_dir: Path) -> Path | None:
    for name in (f"{accession}_family.soft", f"{accession}_family.soft.gz"):
        p = cache_dir / name
        if p.is_file():
            return p
    return None


def _fetch_soft(accession: str, cache_dir: Path) -> Path | None:
    """Pull the SOFT family file from NCBI into ``cache_dir``. Any network
    issue returns ``None`` — the extractor surfaces that as a ``failures``
    entry rather than crashing.

    Tests monkeypatch this function to avoid network calls.
    """
    try:
        import requests  # noqa: F401 — heavy import, only when needed
    except ImportError:
        return None

    # GSE123456 lives under GSE123nnn (thousands bucket).
    m = re.match(r"^(GSE|GDS)(\d+)$", accession)
    if not m:
        return None
    prefix, number = m.group(1), m.group(2)
    bucket = f"{prefix}{number[:-3]}nnn" if len(number) > 3 else f"{prefix}nnn"
    url = f"{_GEO_BASE}/{bucket}/{accession}/soft/{accession}_family.soft.gz"

    dest = cache_dir / f"{accession}_family.soft.gz"
    try:
        import requests
        with requests.get(url, stream=True, timeout=30) as r:
            r.raise_for_status()
            cache_dir.mkdir(parents=True, exist_ok=True)
            with dest.open("wb") as fh:
                for chunk in r.iter_content(chunk_size=1 << 15):
                    if chunk:
                        fh.write(chunk)
        return dest
    except Exception:  # noqa: BLE001 — network / IO / permission — surface as failure
        if dest.exists():
            dest.unlink()
        return None


# ---------- mapping SOFT → PartialDesign ----------

def _first(values: list[str] | None) -> str | None:
    if not values:
        return None
    return values[0]


def _is_url(s: str) -> bool:
    """GEO writes literal 'NONE' (and occasionally empty strings) in
    ``Sample_supplementary_file_N`` when the sample has no processed file at
    the sample level — the actual data sits at ``Series_supplementary_file``
    (a pooled matrix covering all samples, split by barcode suffix). Anything
    that isn't http/https/ftp is noise as far as the downloader is concerned.
    """
    return isinstance(s, str) and s.startswith(("http://", "https://", "ftp://"))


def _extract_characteristics(attrs: dict[str, list[str]]) -> dict[str, ProvenancedValue[str]]:
    """Parse ``Sample_characteristics_ch1`` entries into ``key -> PV(value)``.

    Each entry is ``"key: value"``; multi-colon values keep everything past
    the first colon. Keys are normalized (lowercased, spaces → underscores)
    so downstream consumers don't have to guess casing.
    """
    out: dict[str, ProvenancedValue[str]] = {}
    for entry in attrs.get("Sample_characteristics_ch1", []):
        key, sep, value = entry.partition(":")
        if not sep:
            continue
        norm = key.strip().lower().replace(" ", "_")
        if not norm or norm in out:
            # duplicate key — keep the first to match "first-hit-wins".
            continue
        out[norm] = _pv(
            value.strip(),
            evidence="Sample_characteristics_ch1",
            confidence=0.95,
        )
    return out


def _annotate_feature_types_and_libraries(
    samples: list[PartialSample], url_map: dict[str, list[str]],
) -> None:
    """Populate ``Sample.extra`` with ``feature_type`` / ``library`` /
    ``companion_of`` / ``companion_samples`` based on filename + title
    heuristics. All writes carry ``evidence`` strings so the skill layer
    can audit and, when wrong, override by hand-editing design.yaml.
    """
    # 1. Feature type classification.
    for s in samples:
        title_pv = s.extra.get("title")
        title = str(title_pv.value) if title_pv else ""
        fnames = [u.rsplit("/", 1)[-1] for u in url_map.get(s.sample_id, [])]
        if _looks_like_feature_barcoding(title, fnames):
            s.extra["feature_type"] = _pv(
                "feature_barcoding",
                "heuristic: HASH/MULTI/ADT/HTO/CITE in title or filename",
                confidence=0.85,
            )
        else:
            s.extra["feature_type"] = _pv(
                "gene_expression",
                "heuristic: default (no feature-barcoding signal)",
                confidence=0.75,
            )

    # 2. Library stem extraction.
    stems: dict[str, str] = {}  # sample_id -> stem
    for s in samples:
        fnames = [u.rsplit("/", 1)[-1] for u in url_map.get(s.sample_id, [])]
        if not fnames:
            continue
        # Try every file; first non-None stem wins. Different files of the
        # same library should collapse to the same stem.
        for fname in fnames:
            stem = _library_stem(fname)
            if stem:
                stems[s.sample_id] = stem
                s.extra["library"] = _pv(
                    stem, f"heuristic: stripped suffix from {fname!r}", confidence=0.85,
                )
                break

    # 3. Companion linking: when a library has both a GEX and a
    # feature-barcoding sample, record the cross-refs.
    by_stem: dict[str, list[str]] = {}
    for sid, stem in stems.items():
        by_stem.setdefault(stem, []).append(sid)

    for stem, sids in by_stem.items():
        if len(sids) < 2:
            continue
        gex: list[str] = []
        feat: list[str] = []
        for sid in sids:
            s = next((x for x in samples if x.sample_id == sid), None)
            if s is None:
                continue
            ft = s.extra.get("feature_type")
            if ft and ft.value == "feature_barcoding":
                feat.append(sid)
            else:
                gex.append(sid)
        if not (gex and feat):
            continue
        # Pick the first GEX in the library as the primary; real-world
        # scRNA libraries should only have one anyway.
        primary = gex[0]
        for feat_sid in feat:
            s = next(x for x in samples if x.sample_id == feat_sid)
            s.extra["companion_of"] = _pv(
                primary,
                f"heuristic: shares library stem {stem!r} with a gene_expression sample",
                confidence=0.85,
            )
        s_primary = next(x for x in samples if x.sample_id == primary)
        s_primary.extra["companion_samples"] = _pv(
            "; ".join(sorted(feat)),
            f"heuristic: shares library stem {stem!r} with feature_barcoding samples",
            confidence=0.85,
        )


def _build_partial(parsed: _Parsed, dataset_id: str) -> PartialDesign:
    series = parsed.series
    failures: dict[str, str] = {}

    # Top-level organism. Prefer Series_organism; fall back to any Sample_organism_ch1.
    series_organism = _first(series.get("Series_organism"))
    if series_organism is None:
        for attrs in parsed.samples.values():
            series_organism = _first(attrs.get("Sample_organism_ch1"))
            if series_organism:
                break
    organism_pv = _pv(series_organism, "Series_organism") if series_organism else None
    if organism_pv is None:
        failures["organism"] = "no Series_organism or Sample_organism_ch1 found"

    # Assay: prefer Platform_title of the (first) platform if present. GEO
    # rarely states the wet-lab protocol cleanly, so this is best-effort.
    assay_pv: ProvenancedValue[str] | None = None
    for plat_attrs in parsed.platforms.values():
        title = _first(plat_attrs.get("Platform_title"))
        if title:
            assay_pv = _pv(title, "Platform_title", confidence=0.6)
            break

    # Samples + url_map.
    samples: list[PartialSample] = []
    url_map: dict[str, list[str]] = {}
    for gsm, attrs in parsed.samples.items():
        sample = PartialSample(sample_id=gsm)

        sample.accession = _pv(gsm, "Sample_geo_accession")

        org = _first(attrs.get("Sample_organism_ch1"))
        if org:
            sample.organism = _pv(org, "Sample_organism_ch1")

        # Collect source URLs in order; prefer numbered attributes 1..31.
        urls: list[str] = []
        for i in range(1, 32):
            key = f"Sample_supplementary_file_{i}"
            urls.extend(attrs.get(key, []))
        # Pick up any remaining Sample_supplementary_file_* variants we didn't
        # cover (e.g. non-numbered), preserving SOFT order.
        numbered_keys = {f"Sample_supplementary_file_{i}" for i in range(1, 32)}
        for k in sorted(attrs):
            if k.startswith("Sample_supplementary_file") and k not in numbered_keys:
                urls.extend(attrs[k])

        # Strip GEO's "NONE" placeholder and any other non-URL noise.
        urls = [u for u in urls if _is_url(u)]

        if urls:
            # sample.files is the local relative path under raw/; url_map
            # carries the remote URLs. modes.run downloads url_map[sid][i]
            # to raw/<sid>/<basename(urls[i])>, which lines up with sample.files.
            rel = [f"{gsm}/{u.rsplit('/', 1)[-1]}" for u in urls]
            sample.files = ProvenancedValue(
                value=rel,
                source="geo-soft",
                confidence=0.95,
                evidence="Sample_supplementary_file_*",
            )
            url_map[gsm] = urls

        # Characteristics → extra verbatim. No canonical promotion.
        sample.extra.update(_extract_characteristics(attrs))

        # Title and source_name are verifiable facts but have no canonical slot.
        for attr_key, extra_key in (
            ("Sample_title", "title"),
            ("Sample_source_name_ch1", "source_name"),
            ("Sample_platform_id", "platform_id"),
            ("Sample_library_strategy", "library_strategy"),
            ("Sample_instrument_model", "instrument_model"),
        ):
            v = _first(attrs.get(attr_key))
            if v and extra_key not in sample.extra:
                sample.extra[extra_key] = _pv(v, attr_key)

        samples.append(sample)

    if not samples:
        failures["samples"] = "no ^SAMPLE blocks parsed from SOFT file"

    # Heuristic post-pass: classify feature_type, infer library stems,
    # cross-link GEX ↔ feature-barcoding companions within the same library.
    # All decisions land in Sample.extra with evidence pointers.
    _annotate_feature_types_and_libraries(samples, url_map)

    # Surface series-level supplementary files (common when samples all have
    # Sample_supplementary_file = NONE and the processed matrix is pooled at
    # the series level). Downstream consumers need to split by barcode suffix
    # themselves; standl doesn't auto-split.
    series_supp = [u for u in series.get("Series_supplementary_file", []) if _is_url(u)]
    any_sample_has_files = any(s.files is not None for s in samples)
    if series_supp and not any_sample_has_files:
        failures["data_layout"] = (
            "no sample-level supplementary files; data is pooled at "
            f"Series_supplementary_file ({len(series_supp)} file(s)). "
            "Downstream must split by barcode suffix. URLs: "
            + ", ".join(series_supp)
        )

    notes_parts: list[str] = []
    if title := _first(series.get("Series_title")):
        notes_parts.append(title)
    if series_supp:
        notes_parts.append("series_supplementary_files: " + "; ".join(series_supp))

    return PartialDesign(
        extractor="geo-soft",
        dataset_id=dataset_id,
        source=Source(accessions=[dataset_id], repositories=["GEO"]),
        organism=organism_pv,
        assay=assay_pv,
        samples=samples,
        url_map=url_map,
        failures=failures,
        notes=" | ".join(notes_parts) or None,
    )


# ---------- extractor ----------

class GEOSoftExtractor:
    name = "geo-soft"

    def can_handle(self, source: Source) -> float:
        score = 0.0
        for acc in source.accessions:
            if acc.startswith(("GSE", "GSM", "GPL", "GDS")):
                score = max(score, 0.9)
        if "GEO" in source.repositories:
            score = max(score, 0.8)
        if source.paper_url and "ncbi.nlm.nih.gov/geo" in source.paper_url:
            score = max(score, 0.7)
        return score

    def extract(self, source: Source, cache_dir: Path) -> PartialDesign:
        dataset_id = next(
            (a for a in source.accessions if _GSE_ACCESSION.match(a)),
            None,
        )
        if dataset_id is None:
            return PartialDesign(
                extractor=self.name,
                failures={"accession": "no GSE/GDS accession in Source"},
            )

        cache_dir.mkdir(parents=True, exist_ok=True)
        soft = _locate_soft(dataset_id, cache_dir)
        if soft is None:
            soft = _fetch_soft(dataset_id, cache_dir)
        if soft is None:
            return PartialDesign(
                extractor=self.name,
                dataset_id=dataset_id,
                source=Source(accessions=[dataset_id], repositories=["GEO"]),
                failures={
                    "soft": f"{dataset_id}_family.soft(.gz) not in cache and fetch failed"
                },
            )

        try:
            parsed = _parse_soft(soft)
        except Exception as e:  # noqa: BLE001 — surface any parse failure, don't crash
            return PartialDesign(
                extractor=self.name,
                dataset_id=dataset_id,
                source=Source(accessions=[dataset_id], repositories=["GEO"]),
                failures={"parse": f"{type(e).__name__}: {e}"},
            )

        return _build_partial(parsed, dataset_id)


register(GEOSoftExtractor())
