"""Three entry modes.

``run``         — full pipeline: extract → fetch → validate. Network-heavy.
``validate``    — data already local; cross-check design ↔ raw/ (↔ optional h5ad).
``meta-check``  — data already processed; only verify paper/metadata claims
                  against what's actually in the h5ad. No downloads, no raw/.

All three converge on the same final artifact: ``audit.md`` inside the
dataset directory. That file is the machine-readable + human-readable
source of truth for "is this dataset trustworthy".
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any

import yaml

from datetime import datetime, timezone

from .audit import AuditRecord, AuditReport, Severity, render_markdown
from .merge import merge
from .schema import (
    Design,
    Manifest,
    ManifestEntry,
    PartialDesign,
    PartialSample,
    ProvenancedValue,
    Source,
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------- constants ----------

_SAFE_ID = re.compile(r"^[A-Za-z0-9_.\-]+$")
_ONTOLOGY = re.compile(r"^(CL|UBERON|EFO|MONDO|HANCESTRO|HsapDv|PATO):\d+$")


def _is_safe_relpath(p: str) -> bool:
    """Reject absolute paths, parent-dir escapes, null bytes. Paths must
    resolve *under* raw_dir — extractors from external APIs (BioStudies,
    GEO, ...) are not trusted to hand us clean paths.
    """
    if not p or "\x00" in p:
        return False
    if p.startswith("/") or p.startswith("\\"):
        return False
    # Normalize separators and reject any component that's .. or absolute-ish.
    parts = p.replace("\\", "/").split("/")
    for part in parts:
        if part in ("..", ""):
            return False
    return True


def _ok(check: str, msg: str, evidence: dict[str, Any] | None = None) -> AuditRecord:
    return AuditRecord(check=check, status=Severity.OK, message=msg, evidence=evidence)


def _warn(check: str, msg: str, evidence: dict[str, Any] | None = None) -> AuditRecord:
    return AuditRecord(check=check, status=Severity.WARN, message=msg, evidence=evidence)


def _fail(check: str, msg: str, evidence: dict[str, Any] | None = None) -> AuditRecord:
    return AuditRecord(check=check, status=Severity.FAIL, message=msg, evidence=evidence)


# ---------- loaders ----------

def _load_design(dataset_dir: Path) -> Design:
    return Design.model_validate(yaml.safe_load((dataset_dir / "design.yaml").read_text()))


def _load_manifest(dataset_dir: Path) -> Manifest:
    return Manifest.model_validate(json.loads((dataset_dir / "manifest.json").read_text()))


def _sha256_file(p: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with p.open("rb") as fh:
        for block in iter(lambda: fh.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


# ---------- individual checks ----------

def _check_files_in_manifest(design: Design, manifest: Manifest) -> list[AuditRecord]:
    by_path = {e.path: e for e in manifest.entries}
    recs: list[AuditRecord] = []
    offense = False
    for s in design.samples:
        for f in s.files:
            entry = by_path.get(f)
            if entry is None:
                offense = True
                recs.append(_fail(
                    "files_in_manifest",
                    f"sample {s.sample_id!r} references {f!r} but no manifest entry exists",
                    {"sample_id": s.sample_id, "path": f},
                ))
            elif entry.status != "ok":
                offense = True
                recs.append(_fail(
                    "files_in_manifest",
                    f"manifest entry for {f!r} has status={entry.status!r} (expected 'ok')",
                    {"sample_id": s.sample_id, "path": f, "status": entry.status},
                ))
    if not offense:
        recs.append(_ok("files_in_manifest", "every sample file has a manifest entry with status=ok"))
    return recs


def _check_files_on_disk(dataset_dir: Path, manifest: Manifest, deep: bool) -> list[AuditRecord]:
    raw = dataset_dir / "raw"
    recs: list[AuditRecord] = []
    offense = False
    for e in manifest.entries:
        p = raw / e.path
        if not p.exists():
            offense = True
            recs.append(_fail(
                "files_on_disk",
                f"manifest references {e.path!r} but file not found under raw/",
                {"path": e.path},
            ))
            continue
        if e.size_bytes is not None and p.stat().st_size != e.size_bytes:
            offense = True
            recs.append(_fail(
                "files_on_disk",
                f"size mismatch for {e.path!r}: disk={p.stat().st_size} manifest={e.size_bytes}",
                {"path": e.path, "size_disk": p.stat().st_size, "size_manifest": e.size_bytes},
            ))
            continue
        if deep and e.sha256:
            got = _sha256_file(p)
            if got != e.sha256:
                offense = True
                recs.append(_fail(
                    "files_on_disk",
                    f"sha256 mismatch for {e.path!r}",
                    {"path": e.path, "sha256_disk": got, "sha256_manifest": e.sha256},
                ))
    if not offense:
        mode = "deep (sha256)" if deep else "shallow (size)"
        recs.append(_ok("files_on_disk", f"all manifest files present; {mode} integrity ok"))
    return recs


def _check_no_orphan_raw(dataset_dir: Path, manifest: Manifest) -> list[AuditRecord]:
    raw = dataset_dir / "raw"
    if not raw.exists():
        return [_ok("no_orphan_raw", "no raw/ directory present; skipped")]
    referenced = {e.path for e in manifest.entries}
    recs: list[AuditRecord] = []
    orphans: list[str] = []
    for p in raw.rglob("*"):
        if p.is_file():
            rel = str(p.relative_to(raw))
            if rel not in referenced:
                orphans.append(rel)
    if orphans:
        for o in orphans:
            recs.append(_fail(
                "no_orphan_raw",
                f"file {o!r} exists under raw/ but is not referenced by any manifest entry",
                {"path": o},
            ))
    else:
        recs.append(_ok("no_orphan_raw", "no orphan files under raw/"))
    return recs


def _check_sample_id_valid(design: Design) -> list[AuditRecord]:
    recs: list[AuditRecord] = []
    seen: dict[str, int] = {}
    for s in design.samples:
        seen[s.sample_id] = seen.get(s.sample_id, 0) + 1
    dupes = [sid for sid, n in seen.items() if n > 1]
    offense = False
    for sid in dupes:
        offense = True
        recs.append(_fail(
            "sample_id_valid",
            f"sample_id {sid!r} appears {seen[sid]} times",
            {"sample_id": sid, "count": seen[sid]},
        ))
    for s in design.samples:
        if not s.sample_id or not _SAFE_ID.match(s.sample_id):
            offense = True
            recs.append(_fail(
                "sample_id_valid",
                f"sample_id {s.sample_id!r} is not filesystem-safe "
                "(allowed: [A-Za-z0-9_.-], no '/' or '..')",
                {"sample_id": s.sample_id},
            ))
        for fp in (s.files or []):
            if not _is_safe_relpath(fp):
                offense = True
                recs.append(_fail(
                    "sample_id_valid",
                    f"file path {fp!r} on sample {s.sample_id!r} escapes raw/ "
                    "(absolute, contains '..', or has null bytes)",
                    {"sample_id": s.sample_id, "path": fp},
                ))
    if not offense:
        recs.append(_ok("sample_id_valid", f"all {len(design.samples)} sample_ids unique and filesystem-safe"))
    return recs


def _check_contrasts_valid(design: Design) -> list[AuditRecord]:
    recs: list[AuditRecord] = []
    factors = {f.name: set(f.levels) for f in design.factors}
    if not design.contrasts:
        return [_ok("contrasts_valid", "no contrasts declared; nothing to check")]
    offense = False
    for c in design.contrasts:
        for side_name, side in (("numerator", c.numerator), ("denominator", c.denominator)):
            for factor_name, level in side.items():
                if factor_name not in factors:
                    offense = True
                    recs.append(_fail(
                        "contrasts_valid",
                        f"contrast {c.name!r} {side_name} references undeclared factor {factor_name!r}",
                        {"contrast": c.name, "factor": factor_name},
                    ))
                elif level not in factors[factor_name]:
                    offense = True
                    recs.append(_fail(
                        "contrasts_valid",
                        f"contrast {c.name!r} {side_name} references undeclared level "
                        f"{level!r} of factor {factor_name!r}",
                        {"contrast": c.name, "factor": factor_name, "level": level,
                         "declared": sorted(factors[factor_name])},
                    ))
    if not offense:
        recs.append(_ok("contrasts_valid", f"all {len(design.contrasts)} contrast(s) reference declared factors/levels"))
    return recs


def _check_no_confound(design: Design) -> list[AuditRecord]:
    """Warn-only. A 'perfect confound' between ``condition`` and a batch-like
    column means every sample sharing a batch value also shares a condition
    value AND every condition value corresponds to exactly one batch — no
    within-batch variance in condition. Under that regime no statistical
    test can separate condition from batch.
    """
    recs: list[AuditRecord] = []
    batch_cols = ["batch", "donor_id"]
    rows = [
        {"condition": s.condition, **{c: getattr(s, c) for c in batch_cols}}
        for s in design.samples
    ]
    conditions = {r["condition"] for r in rows if r["condition"] is not None}
    if len(conditions) <= 1:
        return [_ok("no_confound", "fewer than two condition levels; confound check N/A")]

    offense = False
    for col in batch_cols:
        values = [r[col] for r in rows if r[col] is not None and r["condition"] is not None]
        if len(values) < 2:
            continue
        pairs = {(r["condition"], r[col]) for r in rows
                 if r[col] is not None and r["condition"] is not None}
        unique_batch = {r[col] for r in rows if r[col] is not None and r["condition"] is not None}
        # Perfect confound: as many (cond, batch) pairs as there are distinct
        # batches — i.e. each batch maps to exactly one condition.
        if len(pairs) == len(unique_batch) and len(unique_batch) > 1:
            offense = True
            recs.append(_warn(
                "no_confound",
                f"condition is perfectly confounded with {col!r}",
                {"column": col, "mapping": sorted({f"{b}→{c}" for c, b in pairs})},
            ))
    if not offense:
        recs.append(_ok("no_confound", "no perfect confound between condition and batch/donor_id"))
    return recs


def _check_ontology_format(design: Design) -> list[AuditRecord]:
    recs: list[AuditRecord] = []
    offense = False
    for s in design.samples:
        if s.tissue_ontology is None:
            continue
        if not _ONTOLOGY.match(s.tissue_ontology):
            offense = True
            recs.append(_fail(
                "ontology_format",
                f"sample {s.sample_id!r} tissue_ontology {s.tissue_ontology!r} "
                "does not match expected prefix:ID pattern (CL/UBERON/EFO/MONDO)",
                {"sample_id": s.sample_id, "value": s.tissue_ontology},
            ))
    if not offense:
        recs.append(_ok(
            "ontology_format",
            "all ontology terms match expected prefix:ID pattern",
        ))
    return recs


def _load_anndata_or_none(h5ad_path: Path) -> tuple[Any, str | None]:
    """Open ``h5ad_path`` once. Returns ``(anndata, None)`` on success or
    ``(None, skip_reason)`` when anndata is unavailable / the file is
    unreadable. Callers fan out to the individual h5ad checks, each of
    which accepts the shared AnnData so the on-disk file is read exactly
    once per ``validate()`` invocation — previously both checks called
    ``ad.read_h5ad`` independently, doubling IO + memory on GB-scale files.
    """
    try:
        import anndata as ad
    except ImportError:
        return None, "anndata not installed; check skipped"
    try:
        return ad.read_h5ad(h5ad_path), None
    except Exception as e:  # noqa: BLE001 — corrupt file / permission / etc.
        return None, f"failed to read h5ad: {type(e).__name__}: {e}"


def _check_h5ad_samples_match(
    design: Design, h5ad_path: Path, a: Any | None, skip_reason: str | None,
) -> list[AuditRecord]:
    if a is None:
        return [_warn(
            "h5ad_samples_match",
            skip_reason or "h5ad unavailable; check skipped",
            {"h5ad": str(h5ad_path)},
        )]
    if "sample" not in a.obs.columns:
        return [_fail(
            "h5ad_samples_match",
            f"h5ad {h5ad_path.name} has no obs['sample'] column",
            {"h5ad": str(h5ad_path), "obs_columns": list(a.obs.columns)},
        )]
    obs_ids = set(map(str, a.obs["sample"].unique()))
    design_ids = {s.sample_id for s in design.samples}
    missing = design_ids - obs_ids
    extra = obs_ids - design_ids
    recs: list[AuditRecord] = []
    if missing:
        recs.append(_fail(
            "h5ad_samples_match",
            f"{len(missing)} design sample_id(s) absent from h5ad obs['sample']",
            {"missing": sorted(missing)},
        ))
    if extra:
        recs.append(_fail(
            "h5ad_samples_match",
            f"{len(extra)} h5ad obs['sample'] value(s) absent from design",
            {"extra": sorted(extra)},
        ))
    if not recs:
        recs.append(_ok(
            "h5ad_samples_match",
            f"obs['sample'] set matches design.samples ({len(design_ids)} samples)",
        ))
    return recs


def _check_h5ad_cell_count(
    h5ad_path: Path,
    a: Any | None,
    skip_reason: str | None,
    expected: int | None,
    tolerance: float,
) -> list[AuditRecord]:
    if a is None:
        return [_warn(
            "h5ad_cell_count",
            skip_reason or "h5ad unavailable; check skipped",
            {"h5ad": str(h5ad_path)},
        )]
    if expected is None:
        return [_ok("h5ad_cell_count", "no expected cell count provided; check skipped")]
    got = a.n_obs
    lo = expected * (1 - tolerance)
    hi = expected * (1 + tolerance)
    if not (lo <= got <= hi):
        return [_fail(
            "h5ad_cell_count",
            f"h5ad has {got} cells, outside ±{int(tolerance * 100)}% of expected {expected}",
            {"expected": expected, "got": got, "tolerance": tolerance},
        )]
    return [_ok(
        "h5ad_cell_count",
        f"h5ad cell count {got} within ±{int(tolerance * 100)}% of expected {expected}",
    )]


# ---------- entry points ----------

def _build_validate_report(
    dataset_dir: Path,
    h5ad: Path | None = None,
    *,
    deep: bool = False,
    expected_cell_count: int | None = None,
    cell_count_tolerance: float = 0.1,
) -> AuditReport:
    """Same checks as :func:`validate`, but DOES NOT write audit.md. Used by
    callers (``modes.run``) that need to augment the report with their own
    records before materialising the markdown.
    """
    design = _load_design(dataset_dir)
    manifest = _load_manifest(dataset_dir)
    report = AuditReport(dataset_id=design.dataset_id)

    for rec in _check_files_in_manifest(design, manifest):
        report.add(rec)
    for rec in _check_files_on_disk(dataset_dir, manifest, deep=deep):
        report.add(rec)
    for rec in _check_no_orphan_raw(dataset_dir, manifest):
        report.add(rec)
    for rec in _check_sample_id_valid(design):
        report.add(rec)
    for rec in _check_contrasts_valid(design):
        report.add(rec)
    for rec in _check_no_confound(design):
        report.add(rec)
    for rec in _check_ontology_format(design):
        report.add(rec)

    if h5ad is not None:
        a, skip_reason = _load_anndata_or_none(h5ad)
        for rec in _check_h5ad_samples_match(design, h5ad, a, skip_reason):
            report.add(rec)
        for rec in _check_h5ad_cell_count(
            h5ad, a, skip_reason, expected_cell_count, cell_count_tolerance,
        ):
            report.add(rec)

    return report


def validate(
    dataset_dir: Path,
    h5ad: Path | None = None,
    *,
    deep: bool = False,
    expected_cell_count: int | None = None,
    cell_count_tolerance: float = 0.1,
) -> AuditReport:
    """Design ↔ manifest ↔ (optional) h5ad. Writes ``audit.md``.

    Checks (each -> ok/warn/fail with evidence):
      1. every sample.files[*] has manifest entry with status=ok
      2. manifest files exist on disk with matching checksum (cheap: size; deep: sha256)
      3. no orphan files in raw/ not referenced by any sample
      4. sample_id uniqueness + filesystem-safety
      5. contrasts reference declared factors/levels
      6. condition not perfectly confounded with batch/donor_id (warn only)
      7. ontology terms (CL/UBERON/EFO) resolve (if provided)
      8. if h5ad given: obs['sample'] unique values == design.samples sample_ids
      9. if h5ad given: obs cell count within tolerance of paper-stated count
    """
    report = _build_validate_report(
        dataset_dir,
        h5ad=h5ad,
        deep=deep,
        expected_cell_count=expected_cell_count,
        cell_count_tolerance=cell_count_tolerance,
    )
    (dataset_dir / "audit.md").write_text(render_markdown(report))
    return report


def _write_design_yaml(out_dir: Path, design: Design) -> None:
    (out_dir / "design.yaml").write_text(
        yaml.safe_dump(
            design.model_dump(mode="json", exclude_none=True),
            sort_keys=False,
        )
    )


def _write_provenance_json(out_dir: Path, provenance) -> None:
    (out_dir / "provenance.json").write_text(
        json.dumps(
            provenance.model_dump(mode="json", exclude_none=True),
            indent=2,
        ) + "\n"
    )


def _write_manifest_json(out_dir: Path, manifest: Manifest) -> None:
    (out_dir / "manifest.json").write_text(
        json.dumps(
            manifest.model_dump(mode="json", exclude_none=True),
            indent=2,
        ) + "\n"
    )


def _union_url_map(partials: list[PartialDesign]) -> dict[str, list[str]]:
    """Union url_maps from all partials, preserving order within each list."""
    merged: dict[str, list[str]] = {}
    for p in partials:
        for sid, urls in p.url_map.items():
            existing = merged.setdefault(sid, [])
            for u in urls:
                if u not in existing:
                    existing.append(u)
    return merged


def _collect_file_meta(partials: list[PartialDesign]) -> dict[str, dict[str, dict]]:
    """Fold each partial's ``file_meta`` into a lookup ``sid -> url -> meta``.

    ``PartialDesign.file_meta`` is positional (parallel to ``url_map``). Folding
    to a URL-keyed dict makes it trivial to stamp ``ManifestEntry`` after the
    url_map has been unioned — entries keep their source-API checksum even when
    multiple extractors contributed.
    """
    out: dict[str, dict[str, dict]] = {}
    for p in partials:
        for sid, metas in p.file_meta.items():
            urls = p.url_map.get(sid, [])
            if len(metas) != len(urls):
                # Positional misalignment → skip the broken entry but keep
                # whatever we already collected. Defensive against future
                # extractor bugs; never the happy path.
                continue
            bucket = out.setdefault(sid, {})
            for url, meta in zip(urls, metas):
                if meta and url not in bucket:
                    bucket[url] = dict(meta)
    return out


_EXTRACTOR_CACHE_GLOBS = (
    "*_family.soft", "*_family.soft.gz",
    "cellxgene_datasets_index.json",
    "hca_project_*.json",
    "zenodo_*.json",
    "figshare_*.json",
    "biostudies_*.json",
)


def _clear_extractor_caches(paper_cache: Path) -> int:
    """Delete known per-extractor cache files under ``paper_cache``. Returns
    how many files were removed. Leaves anything else alone (skill-flow
    PDFs, hand-stashed supplementary tables, etc.).
    """
    if not paper_cache.exists():
        return 0
    removed = 0
    for pattern in _EXTRACTOR_CACHE_GLOBS:
        for p in paper_cache.glob(pattern):
            try:
                p.unlink()
                removed += 1
            except OSError:
                pass
    return removed


def run(source: Source, out_dir: Path, *, refresh: bool = False) -> AuditReport:
    """Full pipeline: extract design → fetch raw/ → validate → audit.md.

    1. ``pick_extractors(source)`` — run each (NOT h5ad-observed; there's no
       local h5ad yet), collect PartialDesigns. Raises/NotImplementedError
       from an extractor are caught and recorded as a partial with failures.
    2. ``merge()`` — produce one Design + ProvenanceRecord.
    3. Build ``manifest.json`` from the union of per-extractor ``url_map``
       entries, zipped with ``sample.files`` (which carry the predictable
       ``<sample_id>/<basename>`` relative paths).
    4. ``fetch.download`` each entry; update status + sha256 + size in place.
    5. Write design.yaml, provenance.json, manifest.json.
    6. Call ``validate`` to produce audit.md and return the AuditReport.
    """
    from .extractors import pick_extractors
    from .fetch import ChecksumMismatch, download

    out_dir.mkdir(parents=True, exist_ok=True)
    paper_cache = out_dir / "paper"
    paper_cache.mkdir(parents=True, exist_ok=True)
    if refresh:
        _clear_extractor_caches(paper_cache)
    raw_dir = out_dir / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)

    partials: list[PartialDesign] = []
    for ex, _score in pick_extractors(source):
        if ex.name == "h5ad-observed":
            continue  # not applicable at run-time — needs a processed h5ad
        try:
            partials.append(ex.extract(source, cache_dir=paper_cache))
        except NotImplementedError as e:
            partials.append(PartialDesign(
                extractor=ex.name,
                failures={"extract": f"not yet implemented: {e}"},
            ))
        except Exception as e:  # noqa: BLE001
            partials.append(PartialDesign(
                extractor=ex.name,
                failures={"extract": f"{type(e).__name__}: {e}"},
            ))

    if not partials:
        raise ValueError(
            f"no extractors can handle source {source.model_dump(exclude_none=True)}"
        )

    if not any(p.dataset_id for p in partials):
        partials[0].dataset_id = out_dir.name

    merged_design, provenance = merge(partials)
    url_map = _union_url_map(partials)
    meta_lookup = _collect_file_meta(partials)

    manifest_entries: list[ManifestEntry] = []
    pairing_drift: list[AuditRecord] = []
    for s in merged_design.samples:
        files = list(s.files or [])
        urls = url_map.get(s.sample_id, [])
        per_sample_meta = meta_lookup.get(s.sample_id, {})
        if len(files) != len(urls):
            pairing_drift.append(_warn(
                "files_in_manifest",
                f"sample {s.sample_id!r}: "
                f"{len(files)} file entries vs {len(urls)} URLs — "
                f"pairing truncated to {min(len(files), len(urls))}; "
                "some entries will be silently dropped from the manifest",
                {"sample_id": s.sample_id, "n_files": len(files), "n_urls": len(urls)},
            ))
        # Pair files (relative paths) with their source URLs. Extractors emit
        # aligned lists; shorter of the two wins if they drift.
        for rel, url in zip(files, urls):
            meta = per_sample_meta.get(url, {})
            manifest_entries.append(ManifestEntry(
                path=rel,
                url=url,
                status="pending",
                source_accession=s.accession,
                md5=meta.get("md5"),
                sha256=meta.get("sha256"),
                size_bytes=meta.get("size_bytes"),
            ))

    manifest = Manifest(
        dataset_id=merged_design.dataset_id,
        entries=manifest_entries,
        created_at=_now(),
    )

    # Per-entry download errors collected here; surfaced as audit records
    # after validate() runs so audit.md distinguishes "network/404" from
    # "sha256 mismatch" instead of conflating both as ``status=missing``.
    download_errors: list[tuple[ManifestEntry, str, str]] = []
    for entry in manifest.entries:
        dest = raw_dir / entry.path
        try:
            # Pass the API-asserted sha256 through to fetch so it verifies on
            # download. md5 from the API is carried as informational — fetch
            # doesn't verify it (we only compute sha256 during streaming).
            result = download(entry.url, dest, sha256=entry.sha256)
            entry.size_bytes = result.size_bytes
            entry.sha256 = result.sha256
            entry.status = "ok"
            entry.downloaded_at = _now()
        except ChecksumMismatch as e:
            # Bytes arrived; their sha256 didn't match the upstream-asserted
            # value. Trust failure, not a connectivity one. ChecksumMismatch
            # is deliberately its own class (subclass of IOError) so this
            # branch doesn't swallow ``requests.HTTPError`` — which also
            # inherits from IOError and would otherwise be miscategorised.
            entry.status = "corrupt"
            download_errors.append((entry, "corrupt", f"{type(e).__name__}: {e}"))
        except Exception as e:  # noqa: BLE001
            entry.status = "missing"
            download_errors.append((entry, "missing", f"{type(e).__name__}: {e}"))

    _write_design_yaml(out_dir, merged_design)
    _write_provenance_json(out_dir, provenance)
    _write_manifest_json(out_dir, manifest)

    # Build the report WITHOUT writing, augment with extractor failures, then
    # write audit.md exactly once — avoids an intermediate file write the
    # caller never sees.
    report = _build_validate_report(out_dir)

    # File/URL count mismatch at manifest-assembly time means an extractor
    # emitted misaligned lists; silently truncating via ``zip`` would lose
    # manifest entries with no signal to the user.
    for rec in pairing_drift:
        report.add(rec)

    # Surface extractor partial failures so conditions like "data is pooled at
    # series level, samples all have supp=NONE" (geo-soft's ``data_layout``)
    # don't silently result in an empty raw/ dir with an "all ok" audit.md.
    # ``data_layout`` means the pipeline can't produce what the user asked for
    # — elevate to FAIL so `audit.md` worst_severity reflects reality.
    for p in partials:
        for field_name, reason in sorted(p.failures.items()):
            sev = Severity.FAIL if field_name == "data_layout" else Severity.WARN
            report.add(AuditRecord(
                check="extractor_partial_failure",
                status=sev,
                message=f"{p.extractor!r} could not extract {field_name!r}: {reason}",
                evidence={"extractor": p.extractor, "field": field_name, "reason": reason},
            ))

    # Per-entry download failures as structured audit records — `status=corrupt`
    # (sha256 mismatched upstream) is semantically different from
    # `status=missing` (404 / network / timeout), and audit.md now says so
    # instead of burying both under the files_on_disk "file not found".
    for entry, kind, detail in download_errors:
        report.add(AuditRecord(
            check="download_failed",
            status=Severity.FAIL,
            message=f"{entry.path!r} failed to download ({kind}): {detail}",
            evidence={
                "path": entry.path,
                "url": entry.url,
                "kind": kind,
                "error": detail,
            },
        ))

    (out_dir / "audit.md").write_text(render_markdown(report))
    return report


def _design_to_partial(
    d: Design,
    extractor_name: str = "manual",
    confidence: float = 1.0,
) -> PartialDesign:
    """Promote a finished ``Design`` back into a ``PartialDesign`` so it can
    be fed to the merger alongside fresh extractor outputs. Every optional
    sample / top-level field gets wrapped in a ``ProvenancedValue`` with
    ``source=extractor_name`` and the supplied confidence.
    """
    def pv(v: Any) -> ProvenancedValue[Any]:
        return ProvenancedValue(value=v, source=extractor_name, confidence=confidence)

    sample_fields = (
        "accession", "organism", "tissue", "tissue_ontology",
        "cell_type", "disease", "age", "sex", "donor_id",
        "condition", "timepoint", "replicate", "batch",
    )
    samples: list[PartialSample] = []
    for s in d.samples:
        kwargs: dict[str, Any] = {"sample_id": s.sample_id}
        if s.files:
            kwargs["files"] = pv(list(s.files))
        for field in sample_fields:
            v = getattr(s, field)
            if v is not None:
                kwargs[field] = pv(v)
        ps = PartialSample(**kwargs)
        for k, v in s.extra.items():
            ps.extra[k] = pv(v)
        samples.append(ps)

    return PartialDesign(
        extractor=extractor_name,
        dataset_id=d.dataset_id,
        source=d.source,
        organism=pv(d.organism) if d.organism else None,
        assay=pv(d.assay) if d.assay else None,
        samples=samples,
        factors=list(d.factors),
        contrasts=list(d.contrasts),
        batches=list(d.batches),
        notes=d.notes,
    )


def meta_check(
    dataset_dir: Path,
    paper_source: Source | None = None,
    h5ad: Path | None = None,
    *,
    write_design: bool = False,
    expected_cell_count: int | None = None,
    cell_count_tolerance: float = 0.1,
    refresh: bool = False,
) -> AuditReport:
    """Data already processed; only verify metadata claims. Read-only by default.

    Collects up to three views of the design and merges them:
      - existing ``design.yaml`` (if present), promoted to a ``manual``
        PartialDesign at confidence 1.0;
      - local h5ad observations via the ``h5ad-observed`` extractor;
      - paper extractors fired via ``pick_extractors(paper_source)``,
        best-effort — stubbed / failing extractors get a ``warn``, not a raise.

    Emits ``audit.md`` with per-field conflict records (from the merger's
    provenance), the same sample-level checks validate runs, and — when
    ``h5ad`` is given — the two h5ad reconciliation checks.

    ``design.yaml`` and ``provenance.json`` are only written when
    ``write_design=True`` is passed explicitly.
    """
    from .extractors import all_extractors, pick_extractors

    if refresh:
        _clear_extractor_caches(dataset_dir)

    partials: list[PartialDesign] = []
    paper_failures: list[tuple[str, str]] = []
    existing_design: Design | None = None

    design_path = dataset_dir / "design.yaml"
    if design_path.exists():
        existing_design = _load_design(dataset_dir)
        partials.append(_design_to_partial(existing_design))

    if h5ad is not None:
        h5ad_src = Source(local_h5ad=Path(h5ad))
        for ex in all_extractors():
            if ex.name != "h5ad-observed":
                continue
            if ex.can_handle(h5ad_src) <= 0:
                continue
            try:
                partials.append(ex.extract(h5ad_src, cache_dir=dataset_dir))
            except Exception as e:  # noqa: BLE001 — tolerate any extractor failure
                paper_failures.append((ex.name, f"{type(e).__name__}: {e}"))
            break

    if paper_source is not None:
        for ex, _score in pick_extractors(paper_source):
            if ex.name == "h5ad-observed":
                continue
            try:
                partials.append(ex.extract(paper_source, cache_dir=dataset_dir))
            except NotImplementedError:
                paper_failures.append((ex.name, "not yet implemented"))
            except Exception as e:  # noqa: BLE001
                paper_failures.append((ex.name, f"{type(e).__name__}: {e}"))

    # Decide dataset_id. Priority: existing design, any partial that named one,
    # finally the directory basename as a last-resort synthetic id.
    if existing_design is not None:
        dataset_id = existing_design.dataset_id
    else:
        named = next((p.dataset_id for p in partials if p.dataset_id), None)
        dataset_id = named or dataset_dir.name

    report = AuditReport(dataset_id=dataset_id)

    if not partials:
        report.add(_fail(
            "meta_design_present",
            "no design material available: no design.yaml, no h5ad, no paper_source",
        ))
        (dataset_dir / "audit.md").write_text(render_markdown(report))
        return report

    # merge() refuses if no partial carries a dataset_id. Stamp the first one.
    if not any(p.dataset_id for p in partials):
        partials[0].dataset_id = dataset_id

    merged_design, provenance = merge(partials)

    # Per-field conflicts surface through the merger's provenance record.
    conflicts = [f for f in provenance.fields if f.conflict]
    if conflicts:
        for fp in conflicts:
            report.add(_warn(
                "sources_disagree",
                f"field {fp.path!r} has disagreeing values across sources",
                {
                    "chosen": f"{fp.chosen.value!r} via {fp.chosen.source}",
                    "rejected": [f"{r.value!r} via {r.source}" for r in fp.rejected],
                },
            ))
    else:
        report.add(_ok("sources_disagree", "all overlapping fields agree across sources"))

    # Reuse validate's design-only checks against the merged design.
    for rec in _check_sample_id_valid(merged_design):
        report.add(rec)
    for rec in _check_contrasts_valid(merged_design):
        report.add(rec)
    for rec in _check_no_confound(merged_design):
        report.add(rec)
    for rec in _check_ontology_format(merged_design):
        report.add(rec)

    if h5ad is not None:
        # Compare h5ad against the *existing* design so samples the h5ad
        # contributed via the merger don't silently cover gaps. If there is
        # no existing design, the only sample set we have *is* the merged
        # one, so compare against that as a trivial consistency check.
        compare_against = existing_design if existing_design is not None else merged_design
        a, skip_reason = _load_anndata_or_none(Path(h5ad))
        for rec in _check_h5ad_samples_match(compare_against, Path(h5ad), a, skip_reason):
            report.add(rec)
        for rec in _check_h5ad_cell_count(
            Path(h5ad), a, skip_reason, expected_cell_count, cell_count_tolerance,
        ):
            report.add(rec)

    for name, reason in paper_failures:
        report.add(_warn(
            "paper_extractor_skipped",
            f"extractor {name!r} skipped: {reason}",
            {"extractor": name, "reason": reason},
        ))

    # Surface per-field PartialDesign.failures from each extractor so users see
    # which specific fields a (successful) extractor couldn't fill. Separate from
    # paper_extractor_skipped, which is about extractors that didn't produce a
    # PartialDesign at all.
    for p in partials:
        if not p.failures:
            continue
        for field_name, reason in sorted(p.failures.items()):
            report.add(_warn(
                "extractor_partial_failure",
                f"{p.extractor!r} could not extract {field_name!r}: {reason}",
                {"extractor": p.extractor, "field": field_name, "reason": reason},
            ))

    if write_design:
        (dataset_dir / "design.yaml").write_text(
            yaml.safe_dump(
                merged_design.model_dump(mode="json", exclude_none=True),
                sort_keys=False,
            )
        )
        (dataset_dir / "provenance.json").write_text(
            json.dumps(
                provenance.model_dump(mode="json", exclude_none=True),
                indent=2,
            ) + "\n"
        )

    (dataset_dir / "audit.md").write_text(render_markdown(report))
    return report
