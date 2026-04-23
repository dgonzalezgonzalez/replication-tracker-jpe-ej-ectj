"""Map Zenodo community replication records to papers in the local DB.

Communities:
  - ej-replication-repository
  - ectj-replication-repository

Matching strategy:
  1) Explicit paper DOI in title/description/related identifier fields
  2) Fuzzy title match against journal-specific papers in local `papers` table

Writes rows into `repo_mappings` with:
  - repo_host='zenodo'
  - source='zenodo_community'
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

ZENODO_API = "https://zenodo.org/api/communities/{community}/records"
REQUEST_TIMEOUT = 30
RATE_LIMIT_SLEEP = 0.2
PAGE_SIZE = 100
TITLE_MATCH_THRESHOLD = 0.82
SUBSTRING_MIN_LEN = 24
MAX_RETRIES = 5
BACKOFF_BASE_SECONDS = 1.0

COMMUNITIES = {
    "ej-replication-repository": {"issn": "0013-0133", "label": "The Economic Journal"},
    "ectj-replication-repository": {"issn": "1368-4221", "label": "Econometrics Journal"},
}

DOI_RE = re.compile(r"10\.[0-9]{4,9}/[-._;()/:A-Za-z0-9]+")
TITLE_NOISE = re.compile(r'[^\w\s]')
TITLE_PREFIX = re.compile(
    r"^(?:"
    r"replication\s+(?:package|data|code|files?)\s*(?:for)?[:\-]?\s*|"
    r"data\s+and\s+code\s+for[:\-]?\s*|"
    r"code\s+and\s+data\s+for[:\-]?\s*|"
    r"supplementary\s+materials?\s+for[:\-]?\s*"
    r")+",
    re.IGNORECASE,
)

RAW_OUT_DIR = RAW_DIR / "zenodo_communities"


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
    t = TITLE_PREFIX.sub("", t)
    t = TITLE_NOISE.sub(" ", t)
    t = re.sub(r"\s+", " ", t).strip().lower()
    return t


def similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, a, b).ratio()


def fetch_community_records(session: requests.Session, community: str) -> list[dict[str, Any]]:
    page = 1
    all_hits: list[dict[str, Any]] = []

    while True:
        url = ZENODO_API.format(community=community)
        payload: dict[str, Any] | None = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                resp = session.get(
                    url,
                    params={"page": page, "size": PAGE_SIZE},
                    timeout=REQUEST_TIMEOUT,
                )
                if resp.status_code in {429, 500, 502, 503, 504}:
                    resp.raise_for_status()
                resp.raise_for_status()
                payload = resp.json()
                break
            except (requests.RequestException, ValueError) as exc:
                if attempt == MAX_RETRIES:
                    raise
                sleep_s = BACKOFF_BASE_SECONDS * (2 ** (attempt - 1))
                LOGGER.warning(
                    "Zenodo fetch failed for %s page=%d (attempt %d/%d): %s; retrying in %.1fs",
                    community,
                    page,
                    attempt,
                    MAX_RETRIES,
                    exc,
                    sleep_s,
                )
                time.sleep(sleep_s)

        if payload is None:
            raise RuntimeError(f"Unexpected empty payload for {community} page={page}")

        hits = ((payload.get("hits") or {}).get("hits") or [])
        if not hits:
            break
        all_hits.extend(hits)

        if len(hits) < PAGE_SIZE:
            break
        page += 1
        time.sleep(RATE_LIMIT_SLEEP)

    return all_hits


def load_papers_by_issn(conn, issn: str) -> tuple[dict[str, str], list[tuple[str, str, str]]]:
    rows = conn.execute(
        """
        SELECT doi, title
        FROM papers
        WHERE doi IS NOT NULL
          AND title IS NOT NULL
          AND journal_issn = ?
        """,
        (issn,),
    ).fetchall()
    by_doi = {r["doi"]: r["title"] for r in rows}
    title_rows = [(r["doi"], r["title"], normalize_title(r["title"])) for r in rows]
    return by_doi, title_rows


def pick_best_title_match(record_title: str, paper_titles: list[tuple[str, str, str]]) -> tuple[str, float] | None:
    norm = normalize_title(record_title)
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
        SELECT ?, ?, NULL, 'zenodo', 'zenodo_community'
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
        with conn:
            conn.execute("DELETE FROM repo_mappings WHERE source = 'zenodo_community'")

        stats = {
            "records": 0,
            "matched_by_doi": 0,
            "matched_by_title": 0,
            "inserted": 0,
            "unmatched": 0,
        }
        report_rows: list[dict[str, Any]] = []

        with build_session() as session:
            for community, meta in COMMUNITIES.items():
                issn = meta["issn"]
                doi_lookup, paper_titles = load_papers_by_issn(conn, issn)
                LOGGER.info("%s papers loaded: %d", community, len(doi_lookup))

                hits = fetch_community_records(session, community)
                (RAW_OUT_DIR / f"{community}_records.json").write_text(
                    json.dumps(hits, indent=2), encoding="utf-8"
                )
                LOGGER.info("%s records fetched: %d", community, len(hits))

                for idx, hit in enumerate(hits, 1):
                    stats["records"] += 1
                    metadata = hit.get("metadata") or {}
                    repo_doi = normalize_doi(hit.get("doi") or metadata.get("doi"))
                    if not repo_doi:
                        rid = hit.get("id")
                        if rid:
                            repo_doi = f"10.5281/zenodo.{rid}"
                        else:
                            stats["unmatched"] += 1
                            continue

                    title = metadata.get("title") or ""
                    description = metadata.get("description") or ""

                    row: dict[str, Any] = {
                        "community": community,
                        "repo_doi": repo_doi,
                        "title": title,
                        "matched_paper_doi": None,
                        "match_method": None,
                        "score": None,
                    }

                    doi_candidates = set()
                    doi_candidates.update(extract_dois_from_obj(metadata.get("related_identifiers")))
                    doi_candidates.update(extract_dois_from_obj(metadata.get("relations")))
                    doi_candidates.update(extract_dois_from_obj(title))
                    doi_candidates.update(extract_dois_from_obj(description))
                    doi_candidates = {
                        d
                        for d in doi_candidates
                        if not d.startswith("10.5281/zenodo.")
                    }

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

                    if idx % 100 == 0:
                        LOGGER.info(
                            "%s progress %d/%d | doi=%d title=%d inserted=%d unmatched=%d",
                            community,
                            idx,
                            len(hits),
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
        LOGGER.info("Zenodo community mapping complete")
        for k, v in stats.items():
            LOGGER.info("  %-16s %d", k, v)
        LOGGER.info("Report: %s", RAW_OUT_DIR / "match_report.json")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
