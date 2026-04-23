"""Conservative Zenodo metadata upgrade for no-README repos.

Only upgrades a Zenodo repo from unanalyzed -> all_data when metadata text is
explicit and non-restrictive. This script intentionally minimizes false
positives and does not assign partial_data/no_data from metadata.
"""

from __future__ import annotations

import argparse
import html
import json
import logging
import re
import subprocess
import sys
import time
from pathlib import Path

import requests

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

try:
    from config import RAW_DIR, RESTRICTION_INDICATORS
    from db import get_connection, init_db
except ImportError:  # pragma: no cover
    from scripts.config import RAW_DIR, RESTRICTION_INDICATORS
    from scripts.db import get_connection, init_db

LOGGER = logging.getLogger(__name__)
ZENODO_CACHE_DIR = RAW_DIR / "zenodo_communities"
ZENODO_RECORD_RE = re.compile(r"10\.5281/zenodo\.(\d+)", re.IGNORECASE)
REQUEST_TIMEOUT = 30
RATE_LIMIT_SLEEP = 0.2

INCLUSION_VERB_RE = re.compile(r"\b(includes?|contains?|provides?)\b", re.IGNORECASE)
DATA_ARTIFACT_RE = re.compile(
    r"\b(data|dataset|datasets|raw input data|input data|replication data)\b",
    re.IGNORECASE,
)
REPRO_ARTIFACT_RE = re.compile(
    r"\b(code|stata|python|matlab|r code|scripts?|tables?|figures?|results?)\b",
    re.IGNORECASE,
)
SENTENCE_SPLIT_RE = re.compile(r"(?<=[\.\!\?;])\s+|\n+")


def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )


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


def strip_html(text: str | None) -> str:
    if not text:
        return ""
    no_tags = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", html.unescape(no_tags)).strip()


def record_id_from_repo_doi(repo_doi: str) -> str | None:
    m = ZENODO_RECORD_RE.search(repo_doi)
    return m.group(1) if m else None


def build_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(
        {
            "Accept": "application/json",
            "User-Agent": "ReplicationTracker/1.0",
        }
    )
    return s


def load_cached_zenodo_records() -> dict[str, dict[str, str]]:
    by_repo_doi: dict[str, dict[str, str]] = {}
    for path in sorted(ZENODO_CACHE_DIR.glob("*_records.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            LOGGER.warning("Could not read %s: %s", path, exc)
            continue
        if not isinstance(payload, list):
            continue
        for hit in payload:
            metadata = hit.get("metadata") or {}
            repo_doi = normalize_doi(hit.get("doi") or metadata.get("doi"))
            if not repo_doi:
                rid = hit.get("id")
                if rid:
                    repo_doi = f"10.5281/zenodo.{rid}"
            if not repo_doi:
                continue
            by_repo_doi[repo_doi] = {
                "title": strip_html(metadata.get("title") or hit.get("title") or ""),
                "description": strip_html(metadata.get("description") or ""),
            }
    return by_repo_doi


def fetch_zenodo_record(session: requests.Session, repo_doi: str) -> dict[str, str] | None:
    rid = record_id_from_repo_doi(repo_doi)
    if not rid:
        return None
    url = f"https://zenodo.org/api/records/{rid}"
    try:
        resp = session.get(url, timeout=REQUEST_TIMEOUT)
        if resp.status_code != 200:
            return None
        payload = resp.json()
    except (requests.RequestException, ValueError):
        return None
    metadata = payload.get("metadata") or {}
    return {
        "title": strip_html(metadata.get("title") or payload.get("title") or ""),
        "description": strip_html(metadata.get("description") or ""),
    }


def has_restriction_signal(text: str) -> bool:
    low = text.lower()
    for p in RESTRICTION_INDICATORS:
        if p.lower() in low:
            return True
    extra = [
        "restricted access",
        "data use agreement",
        "data-use agreement",
        "proprietary",
        "confidential",
        "not publicly available",
    ]
    return any(p in low for p in extra)


def find_explicit_full_data_evidence(text: str) -> str | None:
    if not text:
        return None
    for sentence in SENTENCE_SPLIT_RE.split(text):
        s = sentence.strip()
        if len(s) < 25:
            continue
        if not INCLUSION_VERB_RE.search(s):
            continue
        if not DATA_ARTIFACT_RE.search(s):
            continue
        if not REPRO_ARTIFACT_RE.search(s):
            continue
        return s
    return None


def ensure_metadata_columns(conn) -> None:
    cols = {
        row["name"] for row in conn.execute("PRAGMA table_info(readme_analysis)").fetchall()
    }
    wanted = {
        "metadata_evidence_source": "TEXT",
        "metadata_evidence_snippet": "TEXT",
        "metadata_upgraded_at": "TEXT",
    }
    with conn:
        for name, sql_type in wanted.items():
            if name not in cols:
                conn.execute(f"ALTER TABLE readme_analysis ADD COLUMN {name} {sql_type}")


def get_targets(conn, limit: int | None) -> list[str]:
    rows = conn.execute(
        """
        SELECT DISTINCT rm.repo_doi
        FROM repo_mappings rm
        JOIN replication_scores rs ON rs.paper_doi = rm.paper_doi
        LEFT JOIN readme_analysis ra ON ra.repo_doi = rm.repo_doi
        WHERE rm.repo_host = 'zenodo'
          AND rs.replication_status = 'unanalyzed_repo'
          AND (
                ra.repo_doi IS NULL
                OR COALESCE(ra.has_readme, 0) = 0
                OR ra.readme_text IS NULL
                OR TRIM(ra.readme_text) = ''
              )
        ORDER BY rm.repo_doi
        """
    ).fetchall()
    repo_dois = [r["repo_doi"] for r in rows if r["repo_doi"]]
    return repo_dois[:limit] if limit else repo_dois


def upsert_upgrade(conn, repo_doi: str, source: str, snippet: str) -> None:
    with conn:
        conn.execute(
            """
            INSERT INTO readme_analysis (
                repo_doi,
                repo_host,
                has_readme,
                readme_text,
                restriction_flags,
                restriction_count,
                data_availability,
                metadata_evidence_source,
                metadata_evidence_snippet,
                metadata_upgraded_at
            )
            VALUES (?, 'zenodo', 0, NULL, '[]', 0, 'all_data', ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(repo_doi) DO UPDATE SET
                data_availability = 'all_data',
                metadata_evidence_source = excluded.metadata_evidence_source,
                metadata_evidence_snippet = excluded.metadata_evidence_snippet,
                metadata_upgraded_at = CURRENT_TIMESTAMP
            """,
            (repo_doi, source, snippet[:1000]),
        )


def maybe_recompute_scores(skip: bool) -> None:
    if skip:
        return
    cmd = [sys.executable, "scripts/09_compute_scores.py"]
    LOGGER.info("Recomputing scores: %s", " ".join(cmd))
    completed = subprocess.run(cmd, cwd=PROJECT_ROOT)
    if completed.returncode != 0:
        raise RuntimeError("09_compute_scores.py failed")


def main() -> int:
    configure_logging()
    parser = argparse.ArgumentParser(
        description="Conservative Zenodo metadata-only upgrade for no-README repos"
    )
    parser.add_argument("--limit", type=int, default=None, help="Max repos to inspect")
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="Fetch fresh targeted metadata from Zenodo API (default uses cache only)",
    )
    parser.add_argument("--dry-run", action="store_true", help="Report only; no DB writes")
    parser.add_argument(
        "--no-recompute",
        action="store_true",
        help="Skip running 09_compute_scores.py after updates",
    )
    args = parser.parse_args()

    db_path = init_db()
    conn = get_connection(db_path)
    ensure_metadata_columns(conn)

    try:
        targets = get_targets(conn, args.limit)
        LOGGER.info("Target repos (Zenodo, no-README, unanalyzed papers): %d", len(targets))
        if not targets:
            return 0

        cached = load_cached_zenodo_records()
        session = build_session() if args.refresh else None

        scanned = upgraded = blocked_restriction = no_metadata = no_evidence = 0
        sample_hits: list[tuple[str, str, str]] = []

        for idx, repo_doi in enumerate(targets, 1):
            meta = cached.get(repo_doi)
            source_used = "cache"
            if args.refresh:
                if idx > 1:
                    time.sleep(RATE_LIMIT_SLEEP)
                refreshed = fetch_zenodo_record(session, repo_doi)
                if refreshed:
                    meta = refreshed
                    source_used = "refresh"

            if not meta:
                no_metadata += 1
                continue

            title = (meta.get("title") or "").strip()
            description = (meta.get("description") or "").strip()
            combined = "\n".join(x for x in (description, title) if x)
            if not combined:
                no_metadata += 1
                continue

            scanned += 1
            if has_restriction_signal(combined):
                blocked_restriction += 1
                continue

            # Prefer description evidence; fallback to title.
            snippet = find_explicit_full_data_evidence(description)
            evidence_source = "description"
            if not snippet:
                snippet = find_explicit_full_data_evidence(title)
                evidence_source = "title"
            if not snippet:
                no_evidence += 1
                continue

            if len(sample_hits) < 8:
                sample_hits.append((repo_doi, f"{source_used}:{evidence_source}", snippet[:180]))

            if not args.dry_run:
                upsert_upgrade(conn, repo_doi, f"{source_used}:{evidence_source}", snippet)
            upgraded += 1

        LOGGER.info("=" * 60)
        LOGGER.info("Scanned metadata records: %d", scanned)
        LOGGER.info("Upgrades (all_data):      %d%s", upgraded, " [dry-run]" if args.dry_run else "")
        LOGGER.info("Blocked by restrictions:  %d", blocked_restriction)
        LOGGER.info("No metadata text:         %d", no_metadata)
        LOGGER.info("No strict evidence:       %d", no_evidence)
        if sample_hits:
            LOGGER.info("Sample evidence:")
            for doi, src, snip in sample_hits:
                LOGGER.info("  %s | %s | %s", doi, src, snip)

        if not args.dry_run:
            maybe_recompute_scores(skip=args.no_recompute)

        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
