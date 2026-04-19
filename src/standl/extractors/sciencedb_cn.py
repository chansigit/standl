"""sciencedb-cn — Science Data Bank (CNIC / China Academy of Sciences).

DOIs of the form ``10.57760/sciencedb.<id>`` are CAS/CNIC-hosted datasets
served by <https://www.scidb.cn/>. Unlike Zenodo/Figshare, every SciDB
endpoint — metadata *and* file listing *and* download — is behind a
login gate; there is no unauthenticated read-only API. That makes a
fully-automated extractor impossible without credentials. This extractor
splits the work accordingly:

- **Unauth path (default)** — resolve the DOI via DataCite
  (``api.datacite.org``) for title / creators / publisher / subjects /
  release date. Emit a single-PartialSample PartialDesign with that
  metadata + a ``data_access`` failure record explaining how to proceed.

- **Authed path** — if ``SCIDB_COOKIE`` is set in the environment, use
  it to call SciDB's private ``/api/sdb/dataset/getDataSetFileTree``
  for the file list and build url_map / file_meta normally.
  ``fetch.download`` then carries the same cookie through via the
  request headers (standl's generic downloader doesn't forward
  cookies today, so downloads of SciDB assets from ``modes.run`` may
  still fail; this is documented as a limitation on first release).

To obtain a SCIDB_COOKIE: log into <https://www.scidb.cn/>, open
DevTools → Application → Cookies, copy the session cookie line
(``SESSION=...; JSESSIONID=...``), export as SCIDB_COOKIE.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

from ..schema import PartialDesign, PartialSample, ProvenancedValue, Source
from .base import make_pv, register

_pv = make_pv("sciencedb-cn", default_confidence=0.9)

_DOI_RE = re.compile(r"^10\.57760/sciencedb\.(\d+)$", re.IGNORECASE)
_URL_RE = re.compile(
    r"scidb\.cn/(?:en/)?detail\?(?:[^&]*&)*dataSetId=([a-f0-9]{32})",
    re.IGNORECASE,
)

DATACITE_BASE = "https://api.datacite.org/dois"
SDB_API = "https://www.scidb.cn/api/sdb"


# ---------- source dispatch ----------

def _extract_identifiers(source: Source) -> tuple[str | None, str | None]:
    """Return ``(sciencedb_id, dataSetId)``. One or both may be None."""
    sdb_id: str | None = None
    dsid: str | None = None

    if source.paper_doi:
        m = _DOI_RE.match(source.paper_doi.strip())
        if m:
            sdb_id = m.group(1)

    if source.paper_url:
        m = _URL_RE.search(source.paper_url)
        if m:
            dsid = m.group(1).lower()

    repos = {r.lower() for r in source.repositories}
    if sdb_id is None and (
        "sciencedb" in repos or "sdb" in repos or "sciencedatabank" in repos
    ):
        for acc in source.accessions:
            if acc.isdigit():
                sdb_id = acc
                break
            if re.fullmatch(r"[a-f0-9]{32}", acc.lower()):
                dsid = acc.lower()
                break

    return sdb_id, dsid


# ---------- API client functions (separately patchable in tests) ----------

def _fetch_datacite(doi: str, cache_dir: Path | None) -> dict[str, Any]:
    """Unauth DataCite lookup; caches per-DOI to ``cache_dir``."""
    key = doi.lower().replace("/", "_")
    if cache_dir is not None:
        cached = cache_dir / f"sciencedb_datacite_{key}.json"
        if cached.is_file():
            return json.loads(cached.read_text())

    import requests

    r = requests.get(
        f"{DATACITE_BASE}/{doi}",
        headers={"Accept": "application/vnd.datacite.datacite+json"},
        timeout=20,
    )
    r.raise_for_status()
    data = r.json()
    if cache_dir is not None:
        cache_dir.mkdir(parents=True, exist_ok=True)
        (cache_dir / f"sciencedb_datacite_{key}.json").write_text(json.dumps(data))
    return data


def _fetch_sdb_file_tree(
    dsid: str, cache_dir: Path | None, cookie: str | None,
) -> tuple[list[dict[str, Any]] | None, str | None]:
    """Return ``(file_records, None)`` on success or ``(None, reason)``
    when the call is refused (no cookie / auth fail / schema change).
    """
    if not cookie:
        return None, (
            "SciDB API requires authentication; set SCIDB_COOKIE to a "
            "browser session cookie (SESSION=...; JSESSIONID=...) to enable "
            "file-list retrieval"
        )

    if cache_dir is not None:
        cached = cache_dir / f"sciencedb_files_{dsid}.json"
        if cached.is_file():
            return json.loads(cached.read_text()), None

    import requests

    try:
        r = requests.get(
            f"{SDB_API}/dataset/getDataSetFileTree",
            params={"dataSetId": dsid},
            headers={"Cookie": cookie, "Accept": "application/json"},
            timeout=30,
        )
        r.raise_for_status()
        data = r.json()
    except Exception as e:  # noqa: BLE001
        return None, f"SciDB file-tree fetch failed: {type(e).__name__}: {e}"

    code = data.get("code")
    if code != 200:
        return None, (
            f"SciDB returned code={code!r} message={data.get('message','?')!r}; "
            "cookie likely expired — log in again and refresh SCIDB_COOKIE"
        )

    files = _flatten_sdb_tree(data.get("data"))
    if cache_dir is not None:
        cache_dir.mkdir(parents=True, exist_ok=True)
        (cache_dir / f"sciencedb_files_{dsid}.json").write_text(json.dumps(data))
    return files, None


def _flatten_sdb_tree(tree: Any) -> list[dict[str, Any]]:
    """SciDB's file tree is a nested directory structure; flatten to a
    list of *leaf* file records carrying a ``fileId`` (directories have
    a ``fileName`` but no ``fileId``, and we skip them). Defensive:
    tolerates missing keys and varying child-list key names.
    """
    out: list[dict[str, Any]] = []

    def _walk(node: Any) -> None:
        if isinstance(node, dict):
            # A leaf file node has fileId; bare directories do not.
            if node.get("fileId") and node.get("fileName"):
                out.append(node)
            for key in ("children", "files", "subFiles", "fileList"):
                if key in node:
                    _walk(node[key])
        elif isinstance(node, list):
            for item in node:
                _walk(item)

    _walk(tree)
    return out


# ---------- DataCite parsing ----------

def _first_title(datacite: dict[str, Any]) -> str | None:
    for t in datacite.get("titles") or []:
        if isinstance(t, dict) and t.get("title"):
            return str(t["title"])
    return None


def _creator_names(datacite: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for c in datacite.get("creators") or []:
        if isinstance(c, dict):
            if c.get("name"):
                out.append(str(c["name"]))
            elif c.get("familyName") or c.get("givenName"):
                out.append(f"{c.get('familyName','')} {c.get('givenName','')}".strip())
    return out


def _subject_labels(datacite: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for s in datacite.get("subjects") or []:
        if isinstance(s, dict) and s.get("subject"):
            out.append(str(s["subject"]))
    return out


def _issue_date(datacite: dict[str, Any]) -> str | None:
    for d in datacite.get("dates") or []:
        if isinstance(d, dict) and d.get("dateType") == "Issued":
            return str(d.get("date", ""))
    return None


def _dsid_from_url(url: str) -> str | None:
    m = _URL_RE.search(url)
    return m.group(1).lower() if m else None


# ---------- extractor ----------

class ScienceDataBankExtractor:
    name = "sciencedb-cn"

    def can_handle(self, source: Source) -> float:
        if source.paper_doi and _DOI_RE.match(source.paper_doi.strip()):
            return 0.9
        if source.paper_url and _URL_RE.search(source.paper_url):
            return 0.9
        repos = {r.lower() for r in source.repositories}
        if any(x in repos for x in ("sciencedb", "sdb", "sciencedatabank")):
            if any(a.isdigit() or re.fullmatch(r"[a-f0-9]{32}", a.lower())
                   for a in source.accessions):
                return 0.9
            return 0.5
        if source.paper_url and "scidb.cn" in source.paper_url:
            return 0.4
        return 0.0

    def extract(self, source: Source, cache_dir: Path) -> PartialDesign:
        sdb_id, dsid = _extract_identifiers(source)
        if sdb_id is None and dsid is None:
            return PartialDesign(
                extractor=self.name,
                failures={
                    "identifier": (
                        "no Science Data Bank DOI / dataSetId found; pass "
                        "paper_doi=10.57760/sciencedb.<id>, "
                        "paper_url=https://www.scidb.cn/detail?dataSetId=<32-hex>, or "
                        "repositories=['ScienceDataBank'] with a matching accession"
                    ),
                },
            )

        cache_dir.mkdir(parents=True, exist_ok=True)

        doi = source.paper_doi or (f"10.57760/sciencedb.{sdb_id}" if sdb_id else None)
        datacite: dict[str, Any] | None = None
        failures: dict[str, str] = {}

        if doi:
            try:
                datacite = _fetch_datacite(doi, cache_dir)
            except Exception as e:  # noqa: BLE001
                failures["datacite"] = f"{type(e).__name__}: {e}"

        # Fill in dsid from DataCite's resolved URL if the caller didn't give one.
        if dsid is None and datacite:
            dsid = _dsid_from_url(str(datacite.get("url") or ""))

        dataset_id = sdb_id or dsid or "sciencedb-unknown"
        sample = PartialSample(sample_id=dataset_id)
        sample.accession = _pv(
            dataset_id,
            "sciencedb-cn record id" if sdb_id else "SciDB dataSetId",
            confidence=1.0,
        )

        # Metadata → Sample.extra (traceable + unaffected by download gate).
        if datacite:
            if title := _first_title(datacite):
                sample.extra["title"] = _pv(title, "datacite.titles[0].title")
            if creators := _creator_names(datacite):
                sample.extra["creators"] = _pv("; ".join(creators[:10]), "datacite.creators[].name")
            if subjects := _subject_labels(datacite):
                sample.extra["subjects"] = _pv("; ".join(subjects[:10]), "datacite.subjects[].subject")
            if issued := _issue_date(datacite):
                sample.extra["issued"] = _pv(issued, "datacite.dates[type=Issued].date")
            if pub := datacite.get("publisher"):
                pub_name = pub.get("name") if isinstance(pub, dict) else str(pub)
                if pub_name:
                    sample.extra["publisher"] = _pv(str(pub_name), "datacite.publisher")

        # File list (auth-gated).
        url_map: dict[str, list[str]] = {}
        file_meta: dict[str, list[dict]] = {}
        if dsid:
            cookie = os.environ.get("SCIDB_COOKIE")
            files, err = _fetch_sdb_file_tree(dsid, cache_dir, cookie)
            if err:
                failures["data_access"] = err
            elif files:
                rel: list[str] = []
                urls: list[str] = []
                metas: list[dict] = []
                for f in files:
                    name = f.get("fileName") or f.get("name")
                    fid = f.get("fileId") or f.get("id")
                    if not (name and fid):
                        continue
                    rel.append(f"{dataset_id}/{name}")
                    urls.append(f"https://www.scidb.cn/api/sdb/file/download?fileId={fid}")
                    meta: dict = {}
                    if (sz := f.get("fileSize") or f.get("size")) is not None:
                        try:
                            meta["size_bytes"] = int(sz)
                        except (TypeError, ValueError):
                            pass
                    if (md5 := f.get("fileMd5") or f.get("md5")):
                        meta["md5"] = str(md5)
                    metas.append(meta)
                if rel:
                    sample.files = ProvenancedValue(
                        value=rel, source=self.name, confidence=0.9,
                        evidence="SciDB getDataSetFileTree files[].fileName",
                    )
                    url_map[dataset_id] = urls
                    file_meta[dataset_id] = metas
                else:
                    failures["files"] = "SciDB API returned 0 file records for this dataset"
        else:
            failures["dataSetId"] = "could not resolve dataSetId (DataCite unavailable?)"

        notes = _first_title(datacite) if datacite else None

        return PartialDesign(
            extractor=self.name,
            dataset_id=dataset_id,
            source=Source(
                accessions=[dataset_id],
                repositories=["ScienceDataBank"],
                paper_doi=doi,
            ),
            samples=[sample],
            url_map=url_map,
            file_meta=file_meta,
            failures=failures,
            notes=notes,
        )


register(ScienceDataBankExtractor())
