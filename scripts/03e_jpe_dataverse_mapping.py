"""Map JPE Dataverse deposits to papers in the local DB.

Source collection:
  https://dataverse.harvard.edu/dataverse/JPE

Matching strategy:
  1) Explicit paper DOI found in dataset metadata fields
  2) Fuzzy title match against JPE-family papers in local `papers` table

Writes rows into `repo_mappings` with:
  - repo_host='dataverse'
  - source='jpe_dataverse'
"""

from __future__ import annotations

import json
import logging
import re
import sys
import time
import unicodedata
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

import requests

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

try:
    from config import RAW_DIR, TARGET_JOURNALS
    from db import get_connection, init_db
except ImportError:  # pragma: no cover
    from scripts.config import RAW_DIR, TARGET_JOURNALS
    from scripts.db import get_connection, init_db

LOGGER = logging.getLogger(__name__)

DATAVERSE_BASE = "https://dataverse.harvard.edu"
COLLECTION_ALIAS = "JPE"
REQUEST_TIMEOUT = 30
RATE_LIMIT_SLEEP = 0.25
TITLE_MATCH_THRESHOLD = 0.84
SUBSTRING_MIN_LEN = 28

DOI_RE = re.compile(r"10\.[0-9]{4,9}/[-._;()/:A-Za-z0-9]+")
TITLE_NOISE = re.compile(r'[^\w\s]')
DATASET_TITLE_PREFIX = re.compile(
    r"^(?:"
    r"replication\s+(?:package|data|code|files?)\s*(?:for)?[:\-]?\s*|"
    r"data\s+and\s+code\s+for[:\-]?\s*|"
    r"code\s+and\s+data\s+for[:\-]?\s*|"
    r"supplementary\s+materials?\s+for[:\-]?\s*"
    r")+",
    re.IGNORECASE,
)

RAW_OUT_DIR = RAW_DIR / "jpe_dataverse"


def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )


def build_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "Accept": "application/json",
            "User-Agent": "ReplicationTracker/1.0",
        }
    )
    return session


def list_collection(session: requests.Session) -> list[dict[str, Any]]:
    url = f"{DATAVERSE_BASE}/api/dataverses/{COLLECTION_ALIAS}/contents"
    resp = session.get(url, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    payload = resp.json()
    datasets = [d for d in payload.get("data", []) if d.get("type") == "dataset"]
    LOGGER.info("JPE collection datasets: %d", len(datasets))
    return datasets


def fetch_dataset(session: requests.Session, pid: str) -> dict[str, Any] | None:
    url = f"{DATAVERSE_BASE}/api/datasets/:persistentId"
    resp = session.get(url, params={"persistentId": pid}, timeout=REQUEST_TIMEOUT)
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return (resp.json() or {}).get("data")


def extract_citation_fields(dataset: dict[str, Any]) -> dict[str, Any]:
    latest = dataset.get("latestVersion") or {}
    blocks = latest.get("metadataBlocks") or {}
    citation = blocks.get("citation") or {}
    fields: dict[str, Any] = {}
    for f in citation.get("fields", []):
        fields[f.get("typeName")] = f.get("value")
    return fields


def normalize_doi(doi: str | None) -> str | None:
    if not doi:
        return None
    d = doi.strip().lower()
    for p in (
        "https://doi.org/",
        "http://doi.org/",
        "https://dx.doi.org/",
        "http://dx.doi.org/",
        "doi:",
    ):
        if d.startswith(p):
            d = d[len(p):].strip()
            break
    return d or None


def extract_dois_from_obj(obj: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(obj, str):
        for m in DOI_RE.finditer(obj):
            d = normalize_doi(m.group(0).rstrip(".,;)"))
            if d:
                found.add(d)
    elif isinstance(obj, dict):
        for v in obj.values():
            found.update(extract_dois_from_obj(v))
    elif isinstance(obj, list):
        for v in obj:
            found.update(extract_dois_from_obj(v))
    return found


def normalize_title(title: str) -> str:
    t = unicodedata.normalize("NFKD", title)
    t = "".join(c for c in t if not unicodedata.combining(c))
    t = DATASET_TITLE_PREFIX.sub("", t)
    t = TITLE_NOISE.sub(" ", t)
    t = re.sub(r"\s+", " ", t).strip().lower()
    return t


def similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, a, b).ratio()


def load_jpe_papers(conn) -> tuple[dict[str, str], list[tuple[str, str, str]]]:
    jpe_issns = {
        j["issn"]
        for j in TARGET_JOURNALS
        if j["display_name"].startswith("Journal of Political Economy")
    }
    placeholders = ",".join("?" * len(jpe_issns))
    rows = conn.execute(
        f"""
        SELECT doi, title
        FROM papers
        WHERE doi IS NOT NULL
          AND title IS NOT NULL
          AND journal_issn IN ({placeholders})
        """,
        tuple(sorted(jpe_issns)),
    ).fetchall()

    by_doi = {r["doi"]: r["title"] for r in rows}
    title_rows = [(r["doi"], r["title"], normalize_title(r["title"])) for r in rows]
    return by_doi, title_rows


def pick_best_title_match(dataset_title: str, paper_titles: list[tuple[str, str, str]]) -> tuple[str, float] | None:
    norm = normalize_title(dataset_title)
    if not norm:
        return None

    best: tuple[str, float] | None = None
    for doi, _orig, pt in paper_titles:
        if not pt:
            continue
        if len(pt) >= SUBSTRING_MIN_LEN and (pt in norm or norm in pt):
            return (doi, 1.0)
        s = similarity(norm, pt)
        if best is None or s > best[1]:
            best = (doi, s)

    if best and best[1] >= TITLE_MATCH_THRESHOLD:
        return best
    return None


def insert_mapping(conn, paper_doi: str, repo_doi: str) -> int:
    cur = conn.execute(
        """
        INSERT INTO repo_mappings (paper_doi, repo_doi, icpsr_project_id, repo_host, source)
        SELECT ?, ?, NULL, 'dataverse', 'jpe_dataverse'
        WHERE EXISTS (SELECT 1 FROM papers WHERE doi = ?)
          AND NOT EXISTS (
              SELECT 1 FROM repo_mappings
              WHERE paper_doi = ?
          )
        """,
        (paper_doi, repo_doi, paper_doi, paper_doi),
    )
    return cur.rowcount


def main() -> int:
    configure_logging()
    RAW_OUT_DIR.mkdir(parents=True, exist_ok=True)

    conn = get_connection(init_db())
    try:
        doi_lookup, paper_titles = load_jpe_papers(conn)
        LOGGER.info("Loaded JPE-family papers: %d", len(doi_lookup))

        with conn:
            conn.execute("DELETE FROM repo_mappings WHERE source = 'jpe_dataverse'")

        report_rows: list[dict[str, Any]] = []
        stats = {
            "datasets": 0,
            "matched_by_doi": 0,
            "matched_by_title": 0,
            "inserted": 0,
            "unmatched": 0,
        }

        with build_session() as session:
            datasets = list_collection(session)
            (RAW_OUT_DIR / "collection_contents.json").write_text(
                json.dumps(datasets, indent=2), encoding="utf-8"
            )

            for idx, d in enumerate(datasets, 1):
                stats["datasets"] += 1
                pid = f"doi:{d['authority']}/{d['identifier']}"
                repo_doi = f"{d['authority']}/{d['identifier']}".lower()
                row: dict[str, Any] = {
                    "repo_doi": repo_doi,
                    "pid": pid,
                    "dataset_title": d.get("name"),
                    "matched_paper_doi": None,
                    "match_method": None,
                    "score": None,
                }

                try:
                    full = fetch_dataset(session, pid)
                except requests.RequestException as exc:
                    row["error"] = str(exc)
                    report_rows.append(row)
                    time.sleep(RATE_LIMIT_SLEEP)
                    continue

                if not full:
                    row["error"] = "dataset_not_found"
                    report_rows.append(row)
                    time.sleep(RATE_LIMIT_SLEEP)
                    continue

                fields = extract_citation_fields(full)
                title = fields.get("title") or d.get("name") or ""
                row["dataset_title"] = title

                doi_candidates = extract_dois_from_obj(fields)
                doi_candidates = {x for x in doi_candidates if not x.startswith("10.7910/dvn/")}

                matched_doi: str | None = None
                for candidate in sorted(doi_candidates):
                    if candidate in doi_lookup:
                        matched_doi = candidate
                        row["matched_paper_doi"] = candidate
                        row["match_method"] = "doi"
                        row["score"] = 1.0
                        stats["matched_by_doi"] += 1
                        break

                if matched_doi is None and title:
                    best = pick_best_title_match(title, paper_titles)
                    if best:
                        matched_doi, score = best
                        row["matched_paper_doi"] = matched_doi
                        row["match_method"] = "title"
                        row["score"] = round(score, 3)
                        stats["matched_by_title"] += 1

                if matched_doi:
                    with conn:
                        inserted = insert_mapping(conn, matched_doi, repo_doi)
                    stats["inserted"] += inserted
                else:
                    stats["unmatched"] += 1

                report_rows.append(row)

                if idx % 25 == 0:
                    LOGGER.info(
                        "Progress %d/%d | doi=%d title=%d inserted=%d unmatched=%d",
                        idx,
                        len(datasets),
                        stats["matched_by_doi"],
                        stats["matched_by_title"],
                        stats["inserted"],
                        stats["unmatched"],
                    )

                time.sleep(RATE_LIMIT_SLEEP)

        (RAW_OUT_DIR / "match_report.json").write_text(
            json.dumps({"stats": stats, "entries": report_rows}, indent=2),
            encoding="utf-8",
        )

        LOGGER.info("=" * 60)
        LOGGER.info("JPE Dataverse mapping complete")
        for k, v in stats.items():
            LOGGER.info("  %-16s %d", k, v)
        LOGGER.info("Report: %s", RAW_OUT_DIR / "match_report.json")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
