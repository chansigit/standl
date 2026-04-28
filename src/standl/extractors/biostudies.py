"""biostudies — EBI BioStudies (and legacy ArrayExpress) extractor.

BioStudies is the EBI deposit hub that absorbed ArrayExpress; studies
carry accessions like ``E-MTAB-<n>`` (historical ArrayExpress scRNA-seq),
``E-GEOD-<n>`` (auto-imports from GEO), ``E-CURD-<n>`` /
``E-HCAD-<n>`` / ``S-BIAD<n>`` / ``S-BSST<n>`` / ``S-SCDT-<...>`` (newer
BioStudies-native).

Two endpoints are needed per study:

- ``GET /api/v1/studies/<acc>`` — tree of attributes + subsections, carries
  Title / Organism / Study type etc. in ``section.attributes``.
- ``GET /api/v1/files/<acc>`` — flat file listing (``data[]``) with
  ``Name``, ``Section`` (raw-data / processed-data / metadata), ``Samples``,
  ``size``, and a ``path`` relative to the study's file root. Download URL
  is ``https://www.ebi.ac.uk/biostudies/files/<acc>/<path>``.

Per ``docs/roadmap.md`` ("Things intentionally deferred: SRA / fastq-level
downloads"), this extractor filters raw-sequencing formats (fastq, bam,
sra, cram, bai) out of ``sample.files`` / ``url_map`` — they're downstream
of the SRA/ENA pipeline, not standl. A study with nothing but raw files
emits a ``data_format`` failure, same shape as geo-soft's ``data_layout``
for pooled series.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from ..schema import PartialDesign, PartialSample, ProvenancedValue, Source
from .base import make_pv, register

_pv = make_pv("biostudies", default_confidence=0.9)

API_BASE = "https://www.ebi.ac.uk/biostudies/api/v1"
FILES_BASE = "https://www.ebi.ac.uk/biostudies/files"

# Accession prefix families we claim. BioStudies' own docs enumerate these —
# keep permissive to new prefixes (S-* and E-*).
_ACCESSION_RE = re.compile(r"^(E-[A-Z]+-[A-Z0-9]+(?:-\d+)?|S-[A-Z0-9]+(?:-[A-Z0-9]+)*)$")
_URL_RE = re.compile(
    r"ebi\.ac\.uk/(?:biostudies|arrayexpress)/(?:studies/)?([A-Z0-9\-]+)",
    re.IGNORECASE,
)

# SRA/ENA raw-sequencing formats. Anything else passes through.
_RAW_EXTENSIONS = {
    ".fastq", ".fastq.gz", ".fq", ".fq.gz",
    ".bam", ".bai", ".cram", ".crai", ".sra",
}


def _extract_accession(source: Source) -> str | None:
    for acc in source.accessions:
        if _ACCESSION_RE.match(acc):
            return acc
    if source.paper_url:
        m = _URL_RE.search(source.paper_url)
        if m and _ACCESSION_RE.match(m.group(1)):
            return m.group(1)
    return None


def _fetch_study(accession: str, cache_dir: Path | None) -> dict[str, Any]:
    """GET /api/v1/studies/<acc>. Caches to ``cache_dir``."""
    if cache_dir is not None:
        cached = cache_dir / f"biostudies_{accession}_study.json"
        if cached.is_file():
            return json.loads(cached.read_text())

    import requests

    r = requests.get(f"{API_BASE}/studies/{accession}", timeout=30)
    r.raise_for_status()
    data = r.json()
    if cache_dir is not None:
        cache_dir.mkdir(parents=True, exist_ok=True)
        (cache_dir / f"biostudies_{accession}_study.json").write_text(json.dumps(data))
    return data


def _fetch_files(accession: str, cache_dir: Path | None) -> dict[str, Any]:
    """GET /api/v1/files/<acc>. Pages with ``pageSize=10000`` to avoid
    pagination for any reasonably-sized study."""
    if cache_dir is not None:
        cached = cache_dir / f"biostudies_{accession}_files.json"
        if cached.is_file():
            return json.loads(cached.read_text())

    import requests

    r = requests.get(f"{API_BASE}/files/{accession}", params={"pageSize": 10000}, timeout=30)
    r.raise_for_status()
    data = r.json()
    if cache_dir is not None:
        cache_dir.mkdir(parents=True, exist_ok=True)
        (cache_dir / f"biostudies_{accession}_files.json").write_text(json.dumps(data))
    return data


def _attrs_to_dict(attrs: Any) -> dict[str, str]:
    out: dict[str, str] = {}
    if not isinstance(attrs, list):
        return out
    for a in attrs:
        if isinstance(a, dict) and a.get("name"):
            v = a.get("value")
            if v is not None:
                out[str(a["name"]).strip().lower()] = str(v)
    return out


def _fetch_sdrf(accession: str, sdrf_path: str,
                cache_dir: Path | None) -> str | None:
    """Download an SDRF text file from the BioStudies file area, with
    optional disk caching. Returns the raw TSV text, or ``None`` on
    fetch failure.
    """
    cache_file = (cache_dir / f"biostudies_{accession}_sdrf.txt"
                  if cache_dir is not None else None)
    if cache_file is not None and cache_file.is_file():
        return cache_file.read_text()

    import requests
    url = f"{FILES_BASE}/{accession}/{sdrf_path}"
    try:
        r = requests.get(url, timeout=30)
        r.raise_for_status()
    except Exception:
        return None
    text = r.text
    if cache_file is not None:
        cache_dir.mkdir(parents=True, exist_ok=True)  # type: ignore[union-attr]
        cache_file.write_text(text)
    return text


_SDRF_BRACKET_RE = re.compile(r"^([A-Za-z ]+?)\s*\[([^\]]+)\]\s*$")


def _normalize_sdrf_key(col: str) -> str | None:
    """
    Map SDRF column headers to compact, snake_case extra keys.

    Handled prefixes (case-insensitive):
      - ``Characteristics[organism part]``  → ``organism_part``
      - ``Factor Value[cell type]``         → ``factor_cell_type``
      - ``Comment[ENA_RUN]`` / ``[ENA_SAMPLE]`` / ``[BioSD_SAMPLE]``
                                            → ``ena_run`` / ``ena_sample``
                                              / ``biosd_sample``
      - ``Source Name`` (special-cased upstream — used as the join key)

    Other ``Comment[*]`` columns (file URLs, library prep parameters
    that aren't downstream-useful) are dropped to keep ``extra`` tidy.

    Returns ``None`` for headers we don't want to surface.
    """
    if col is None:
        return None
    raw = col.strip()
    if not raw:
        return None

    # Source Name is the join key, not a payload column.
    if raw.lower() == "source name":
        return None

    m = _SDRF_BRACKET_RE.match(raw)
    if m:
        prefix = m.group(1).strip().lower()
        inner = m.group(2).strip().lower()
        if prefix == "characteristics":
            return _slugify_sdrf(inner)
        if prefix == "factor value":
            return f"factor_{_slugify_sdrf(inner)}"
        if prefix == "comment":
            # Whitelist Comments we care about; everything else gets
            # dropped to keep extra dictionaries small.
            keep = {
                "ena_run", "ena_sample", "ena_experiment", "ena_study",
                "biosd_sample", "library_layout", "library_strategy",
                "library_source", "library_selection", "instrument",
                "instrument model", "single cell isolation",
            }
            inner_norm = _slugify_sdrf(inner)
            return inner_norm if inner_norm in {_slugify_sdrf(k) for k in keep} \
                else None
        return None

    # Bare headers (no brackets) — slugify but only keep a small set
    # of common ones; SDRF files have lots of free-form columns.
    bare_keep = {"protocol_ref", "term_source_ref", "performer"}
    s = _slugify_sdrf(raw)
    return s if s in bare_keep else None


def _slugify_sdrf(s: str) -> str:
    """SDRF-specific slug: lowercase, spaces → underscore, drop punctuation."""
    out = re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")
    return out or "x"


def _parse_sdrf(text: str) -> list[dict[str, str]]:
    """Parse an SDRF TSV string into a list of row dicts.

    Each dict has its ``Source Name`` value under ``__source_name`` and
    every other column normalised via :func:`_normalize_sdrf_key`.
    Headers we don't want to surface (random ``Comment[...]`` columns)
    are dropped silently. Rows with no ``Source Name`` are dropped — we
    can't join them.
    """
    if not text:
        return []
    lines = text.replace("\r\n", "\n").split("\n")
    if not lines or not lines[0]:
        return []
    headers = [h.strip() for h in lines[0].split("\t")]
    # Find Source Name column index (case-insensitive)
    source_idx = next(
        (i for i, h in enumerate(headers) if h.strip().lower() == "source name"),
        None,
    )
    if source_idx is None:
        return []
    norm_keys = [_normalize_sdrf_key(h) for h in headers]

    rows: list[dict[str, str]] = []
    for line in lines[1:]:
        if not line.strip():
            continue
        cells = line.split("\t")
        if len(cells) <= source_idx:
            continue
        source_name = cells[source_idx].strip()
        if not source_name:
            continue
        row: dict[str, str] = {"__source_name": source_name}
        for key, val in zip(norm_keys, cells):
            if key is None:
                continue
            v = val.strip()
            if not v:
                continue
            # Last write wins on duplicate normalised keys (e.g. multiple
            # Comment columns mapping to the same slug). Same-name SDRF
            # columns are rare; not worth carrying lists.
            row[key] = v
        rows.append(row)
    return rows


def _inject_sdrf_into_samples(
    samples: list[PartialSample], sdrf_rows: list[dict[str, str]]
) -> int:
    """Merge SDRF row-level fields into ``sample.extra``. Returns the
    number of samples that received at least one new key.

    Match strategy (in order):
      1. Exact ``Source Name`` == ``sample.sample_id``.
      2. Slug match: ``_slug(source_name)`` == ``sample.sample_id``.
      3. ENA run / ENA sample == sample_id (covers cases where the
         BioStudies sample_id was derived from the ENA accession).

    Multiple SDRF rows mapping to the same sample (multi-lane runs)
    fold into the same extra dict; on key collision the *first* row's
    value wins, since later rows would just be re-deposits of the same
    biological sample.
    """
    if not sdrf_rows:
        return 0

    # Build a lookup keyed by every plausible identifier
    by_key: dict[str, dict[str, str]] = {}
    for row in sdrf_rows:
        sn = row["__source_name"]
        for key in (sn, _slug(sn)):
            by_key.setdefault(key, row)
        for ena_key in ("ena_run", "ena_sample"):
            v = row.get(ena_key)
            if v:
                by_key.setdefault(v, row)

    n_hit = 0
    for s in samples:
        row = by_key.get(s.sample_id) or by_key.get(_slug(s.sample_id))
        if row is None:
            continue
        added = 0
        for key, val in row.items():
            if key.startswith("__"):
                continue
            if key in s.extra:
                continue  # don't clobber attributes already present
            s.extra[key] = _pv(
                str(val)[:500], f"sdrf.{key}", confidence=0.85,
            )
            added += 1
        if added:
            n_hit += 1
    return n_hit


def _is_raw_only(name: str) -> bool:
    n = name.lower()
    # Check longest suffix first so `.fastq.gz` wins over `.gz`.
    for suffix in (".fastq.gz", ".fq.gz"):
        if n.endswith(suffix):
            return True
    for suffix in _RAW_EXTENSIONS:
        if n.endswith(suffix):
            return True
    return False


_SLUG_UNSAFE = re.compile(r"[^A-Za-z0-9._-]+")


def _slug(label: str) -> str:
    """Slugify a BioStudies ``Samples`` label for use as a sample_id suffix."""
    s = _SLUG_UNSAFE.sub("_", (label or "").strip()).strip("_")
    return s or "unassigned"


def _uniquify(base: str, taken: set[str]) -> str:
    """Return ``base`` if unused; otherwise ``base_2`` / ``base_3`` / … until
    unique. Guards against ``_slug`` collapsing distinct labels (``"Sample 1"``
    and ``"Sample-1"`` both produce ``"Sample_1"``) — without this the second
    PartialSample's url_map would silently overwrite the first.
    """
    if base not in taken:
        taken.add(base)
        return base
    n = 2
    while True:
        candidate = f"{base}_{n}"
        if candidate not in taken:
            taken.add(candidate)
            return candidate
        n += 1


def _group_files_by_samples(
    all_data: list[dict[str, Any]],
) -> tuple[dict[str, list[dict[str, Any]]], int]:
    """Group files by their ``Samples`` field. Returns
    ``(groups, raw_count_total)`` where ``groups`` maps ``Samples`` value
    (empty string retained) → list of non-raw file records."""
    groups: dict[str, list[dict[str, Any]]] = {}
    raw_count = 0
    for f in all_data:
        if not isinstance(f, dict):
            continue
        name = f.get("path") or f.get("Name") or ""
        if not name:
            continue
        if _is_raw_only(name):
            raw_count += 1
            continue
        label = (f.get("Samples") or "").strip()
        groups.setdefault(label, []).append(f)
    return groups, raw_count


class BioStudiesExtractor:
    name = "biostudies"

    def can_handle(self, source: Source) -> float:
        if any(_ACCESSION_RE.match(a) for a in source.accessions):
            return 0.9
        if source.paper_url:
            if "ebi.ac.uk/biostudies" in source.paper_url or "ebi.ac.uk/arrayexpress" in source.paper_url:
                m = _URL_RE.search(source.paper_url)
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
                        "no BioStudies/ArrayExpress accession; pass "
                        "accessions=[<E-MTAB-*|S-BIAD*|...>] or a "
                        "ebi.ac.uk/biostudies/studies/<accession> URL"
                    ),
                },
            )

        cache_dir.mkdir(parents=True, exist_ok=True)
        try:
            study = _fetch_study(accession, cache_dir)
        except Exception as e:  # noqa: BLE001
            return PartialDesign(
                extractor=self.name,
                dataset_id=accession,
                source=Source(accessions=[accession], repositories=["BioStudies"]),
                failures={"api_study": f"{type(e).__name__}: {e}"},
            )
        try:
            files_resp = _fetch_files(accession, cache_dir)
        except Exception as e:  # noqa: BLE001
            return PartialDesign(
                extractor=self.name,
                dataset_id=accession,
                source=Source(accessions=[accession], repositories=["BioStudies"]),
                failures={"api_files": f"{type(e).__name__}: {e}"},
            )

        return _build_partial(study, files_resp, accession, cache_dir)


def _build_sample(
    accession: str, sample_id: str, label: str | None,
    files: list[dict[str, Any]],
    organism_pv: ProvenancedValue[str] | None,
    merged_attrs: dict[str, str],
) -> tuple[PartialSample, list[str], list[dict]]:
    """Build a PartialSample + its url_map + file_meta lists for one group."""
    sample = PartialSample(sample_id=sample_id)
    sample.accession = _pv(accession, "biostudies accession", confidence=1.0)
    if organism_pv:
        sample.organism = organism_pv
    if label:
        sample.extra["biostudies_samples_field"] = _pv(label, "files[*].Samples")
    for key_lc, extra_key in (
        ("title", "title"),
        ("study type", "study_type"),
        ("description", "description"),
        ("releasedate", "release_date"),
    ):
        v = merged_attrs.get(key_lc)
        if v:
            sample.extra[extra_key] = _pv(str(v)[:500], f"attributes.{extra_key}")

    rel_paths: list[str] = []
    urls: list[str] = []
    metas: list[dict] = []
    for f in files:
        path = f.get("path") or f.get("Name")
        rel_paths.append(f"{sample_id}/{path}")
        urls.append(f"{FILES_BASE}/{accession}/{path}")
        meta: dict = {}
        if (sz := f.get("size")) is not None:
            try:
                meta["size_bytes"] = int(sz)
            except (TypeError, ValueError):
                pass
        metas.append(meta)
    sample.files = ProvenancedValue(
        value=rel_paths,
        source="biostudies",
        confidence=0.95,
        evidence="files.data[*].path (raw formats filtered)",
    )
    return sample, urls, metas


def _build_partial(
    study: dict[str, Any], files_resp: dict[str, Any], accession: str,
    cache_dir: Path | None = None,
) -> PartialDesign:
    failures: dict[str, str] = {}

    section = study.get("section") or {}
    sec_attrs = _attrs_to_dict(section.get("attributes"))
    root_attrs = _attrs_to_dict(study.get("attributes"))
    merged = {**root_attrs, **sec_attrs}  # section wins on collisions

    organism_pv = None
    if (org := merged.get("organism")):
        organism_pv = _pv(org, "section.attributes.Organism")

    assay_pv = None
    if (study_type := merged.get("study type")):
        assay_pv = _pv(study_type, "section.attributes.'Study type'", confidence=0.6)

    all_data = files_resp.get("data") or []
    groups, raw_count = _group_files_by_samples(all_data)

    # Drop empty groups, then decide: single vs split.
    non_empty = {k: v for k, v in groups.items() if v}
    labels_with_files = [k for k in non_empty if k]  # non-empty labels only

    samples: list[PartialSample] = []
    url_map: dict[str, list[str]] = {}
    file_meta: dict[str, list[dict]] = {}

    if len(labels_with_files) >= 2:
        # Real per-sample grouping — emit one PartialSample per distinct label.
        # Files with empty Samples (shared / study-level) attach to a sibling
        # "_unassigned" sample so they don't get silently dropped. Slug
        # collisions are resolved with a numeric suffix so we never drop a
        # group.
        taken_slugs: set[str] = set()
        for label, files in non_empty.items():
            base = _slug(label) if label else "unassigned"
            suffix = _uniquify(base, taken_slugs)
            sid = f"{accession}_{suffix}"
            s, urls, metas = _build_sample(accession, sid, label or None, files, organism_pv, merged)
            samples.append(s)
            url_map[sid] = urls
            file_meta[sid] = metas
    elif non_empty:
        # Only one labelled group (or none — just empty-Samples files) —
        # keep the historical behaviour: a single PartialSample keyed by the
        # accession, holding every non-raw file whatever its Samples label.
        merged_files = [f for files in non_empty.values() for f in files]
        label = labels_with_files[0] if labels_with_files else None
        s, urls, metas = _build_sample(
            accession, accession, label, merged_files, organism_pv, merged,
        )
        samples.append(s)
        url_map[accession] = urls
        file_meta[accession] = metas
    else:
        # No processed files at all — either fastq-only or truly empty.
        if all_data:
            failures["data_format"] = (
                f"only raw-sequencing formats available ({raw_count} fastq/bam/sra file(s)); "
                f"SRA/ENA processing is out of scope for standl. See ENA/SRA or a "
                f"processed-matrix deposit."
            )
        else:
            failures["files"] = "no files listed on BioStudies for this study"
        # Still emit a study-level PartialSample so downstream consumers can
        # surface the failure alongside metadata.
        s = PartialSample(sample_id=accession)
        s.accession = _pv(accession, "biostudies accession", confidence=1.0)
        if organism_pv:
            s.organism = organism_pv
        for key_lc, extra_key in (
            ("title", "title"),
            ("study type", "study_type"),
            ("description", "description"),
        ):
            v = merged.get(key_lc)
            if v:
                s.extra[extra_key] = _pv(str(v)[:500], f"attributes.{extra_key}")
        samples.append(s)

    if raw_count:
        for s in samples:
            s.extra["biostudies_raw_file_count"] = _pv(
                str(raw_count), "files.data (fastq/bam/sra excluded)",
            )

    # SDRF rich-column expansion. ArrayExpress / BioStudies ship a
    # tab-separated `*.sdrf.txt` alongside the data files, with
    # Characteristics[*] and Factor Value[*] columns that carry the
    # per-sample biology (organism part, sex, age, disease, instrument
    # …). Without parsing it, downstream stages have no per-sample
    # metadata to broadcast onto cells. We download the file once,
    # parse it, and merge each row into the matching sample's `.extra`.
    sdrf_record = next(
        (
            f for f in all_data if isinstance(f, dict)
            and (n := str(f.get("path") or f.get("Name") or "")).lower().endswith(
                (".sdrf.txt", ".sdrf.tsv", ".sdrf")
            )
        ),
        None,
    )
    if sdrf_record is not None:
        sdrf_path = sdrf_record.get("path") or sdrf_record.get("Name")
        text = _fetch_sdrf(accession, str(sdrf_path), cache_dir)
        if text:
            rows = _parse_sdrf(text)
            n_hit = _inject_sdrf_into_samples(samples, rows)
            if n_hit == 0 and rows:
                failures["sdrf_match"] = (
                    f"sdrf parsed {len(rows)} row(s) but none matched the "
                    f"{len(samples)} BioStudies-derived sample(s) by Source "
                    f"Name / slug / ENA accession"
                )
        elif sdrf_path:
            failures["sdrf_fetch"] = f"could not download {sdrf_path}"

    title = merged.get("title")

    return PartialDesign(
        extractor="biostudies",
        dataset_id=accession,
        source=Source(
            accessions=[accession],
            repositories=["BioStudies"],
        ),
        organism=organism_pv,
        assay=assay_pv,
        samples=samples,
        url_map=url_map,
        file_meta=file_meta,
        failures=failures,
        notes=str(title) if title else None,
    )


register(BioStudiesExtractor())
