# standl roadmap

Current state: **schema + architecture scaffolded, no extractor implemented.**

Design decisions already made (don't re-litigate without cause):

- Extractors are pluggable; core dispatches by `can_handle()` score, runs all
  above threshold, merger reconciles. No `if GSE:` branching in the core.
- `design.yaml` is clean (no inline provenance); per-field attribution lives
  in `provenance.json` sidecar.
- Three CLI modes: `run` (full), `validate` (files already local),
  `meta-check` (only verify metadata, don't overwrite design.yaml).
- GEO is a moving target — `geo-soft` extractor is "wide-in, narrow-out",
  deterministic facts only. Condition/batch/contrast inference belongs to
  `llm-paper`.
- Failures are recorded (`PartialDesign.failures`, `audit.md`), not raised.

## Implementation order

Rationale: build the thing that has no network / LLM dependency first; use
it as the testing substrate for everything that follows.

### 1. `modes.validate` — first to implement

Pure local I/O. No extractors, no network. Good way to exercise schema
round-tripping and the `audit.md` format.

Inputs: a dataset dir with a hand-authored `design.yaml` +
`manifest.json` + `raw/`.

Deliverables:
- `src/standl/audit.py` — ok/warn/fail record type + markdown renderer
- Implement all 9 checks listed in `modes.validate` docstring
- Fixture dataset under `tests/fixtures/` (tiny, synthetic)
- `tests/test_validate.py`

### 2. `modes.meta_check` — adapter on top of validate

Adds one new piece: reading an h5ad's `obs`/`uns` and producing a synthetic
`PartialDesign` to feed the merger. Reuses everything from step 1.

Deliverables:
- `src/standl/extractors/h5ad_observed.py` — an extractor that "extracts"
  from a local h5ad (treat data as a source). Registered like the others.
- Wire `modes.meta_check`: merge (existing design.yaml as manual PartialDesign)
  + (h5ad_observed PartialDesign) + (optional fresh paper re-extraction).
- Tests: design says N samples, h5ad has N-1 → meta-check flags it.

### 3. `geo-soft` extractor — first network extractor

Concrete implementation of the wide-in/narrow-out policy.

Deliverables:
- Try SOFT family file → MINiML XML → series matrix header, first-hit-wins
  per field. Tolerant of drift: missing keys go to `failures`, not exceptions.
- Raw characteristics key/value pairs land in `Sample.extra` verbatim.
- Do **not** parse free-text into `condition`/`batch` — that's step 4's job.
- Fixture: cache a real SOFT file under `tests/fixtures/geo/` and test offline.

### 4. `llm-paper` extractor

Schema-constrained Anthropic tool-use to produce `PartialDesign` from paper
text (PMC XML preferred, PDF fallback).

Deliverables:
- Paper resolution: DOI → PMC XML / bioRxiv / publisher PDF; cache to disk.
- Tool-use call with `PartialDesign` JSON schema as the tool input schema.
- Per-field evidence pointers ("Methods §3", "Table S2 row 4"); confidence
  drops when evidence is missing.
- Prompt caching on the paper text (paper is large, schema is stable).

### 5. `modes.run` — tie it together

At this point `run` is mostly glue:
1. `pick_extractors(source)` → run each → collect PartialDesigns
2. `merge()` → write `design.yaml` + `provenance.json`
3. Resolve `sample.files` → URLs (extractor-provided) → `manifest.json`
4. Download with resume + sha256, update manifest statuses
5. Call `validate`.

Deliverables:
- `src/standl/fetch.py` — resumable HTTP downloader + checksum verification
- URL resolution: extractors optionally return a `sample_id → [urls]` map
  alongside `PartialDesign`

## Things intentionally deferred

- SRA / fastq-level downloads (prefer processed matrices first).
- Authentication for controlled-access (dbGaP, EGA).
- A GUI / notebook interface.
- Caching shared across users (per-user cache is enough to start).

## When in doubt

Re-read `docs/design-schema.md` and `skills/standl/SKILL.md`. If this
roadmap conflicts with those, the schema + skill are authoritative.
