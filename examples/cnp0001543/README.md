# CNP0001543 — CNGBdb demo

Reference output of `standl run CNP0001543` on **Chen et al. 2022**,
*Spatiotemporal transcriptomic atlas of mouse organogenesis using DNA
nanoball patterned arrays* (Stereo-seq embryo atlas, BGI).

Exercises the **`cngbdb`** extractor against `db.cngb.org`.

## What's here

| file | produced by |
|---|---|
| `design.yaml` | `cngbdb` — title + description scraped from the project page's `<head>`; `ftp_hint` pointing at the CNSA FTP base |
| `audit.md` | `modes.validate` — worst severity `warn` |
| `manifest.json` | empty (see note below) |
| `provenance.json` | every PV sourced from `cngbdb`, confidence 0.8–0.95 |

## The warn is intentional

CNGBdb has no public JSON API for per-project metadata, and the CNSA FTP
shard (`data1` … `data9`) is not deterministic from the accession — it's
resolved by client-side JavaScript on the project page. The `cngbdb`
extractor is therefore **metadata-only** by design: it pulls title and
description from the HTML head + tries DataCite for a DOI / organism
hint, and records a failure under `files`:

    WARN — extractor_partial_failure
      'cngbdb' could not extract 'files': CNSA shard not resolvable
      without JS; browse the project page manually

To populate `raw/`, open the `db.cngb.org/search/project/CNP0001543/`
page, copy the FTP paths shown, and fetch them via `ftp.cngb.org`.
Re-run `standl validate` afterward.

## Reproduce

    standl run CNP0001543 -o examples/cnp0001543
