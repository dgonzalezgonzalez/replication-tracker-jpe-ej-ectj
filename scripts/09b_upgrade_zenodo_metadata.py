"""Conservative Zenodo metadata/tree upgrade for no-README repos.

Primary pass: upgrade Zenodo repos from unanalyzed -> all_data using strict metadata
text evidence (description/title).

Fallback pass: for archive-heavy Zenodo records, inspect Zenodo's archive preview tree
(without downloading full archives). If the tree clearly shows README + data files and
code/repro artifacts, upgrade to all_data.
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
from urllib.parse import quote

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
ZENODO_PREVIEW_CACHE_DIR = RAW_DIR / "zenodo_preview_trees"
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
README_RE = re.compile(r"read[\s_-]?me", re.IGNORECASE)
FILE_TEXT_RE = re.compile(r'file outline icon"></i></i>\s*([^<\n\r]+?)\s*</span>', re.IGNORECASE)

ARCHIVE_EXTENSIONS = {
    ".zip",
    ".tar",
    ".gz",
    ".tgz",
    ".bz2",
    ".xz",
    ".7z",
}
DATA_EXTENSIONS = {
    ".csv",
    ".dta",
    ".rdata",
    ".rds",
    ".sav",
    ".xlsx",
    ".xls",
    ".json",
    ".parquet",
    ".feather",
    ".txt",
    ".dat",
    ".tsv",
}
CODE_EXTENSIONS = {
    ".do",
    ".py",
    ".r",
    ".m",
    ".ipynb",
    ".jl",
    ".sas",
    ".mata",
    ".ado",
    ".mod",
    ".inp",
    ".qmd",
}
DATA_NAME_HINTS = (
    "dataset",
    "data",
    "input",
    "raw",
    "sample",
)
CODE_NAME_HINTS = (
    "code",
    "script",
    "analysis",
    "replicate",
    "do_file",
    "do-files",
)


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


def _extract_file_keys(files_payload) -> list[str]:
    out: list[str] = []
    if isinstance(files_payload, list):
        for item in files_payload:
            if not isinstance(item, dict):
                continue
            key = item.get("key") or item.get("filename")
            if isinstance(key, str) and key.strip():
                out.append(key.strip())
    elif isinstance(files_payload, dict):
        entries = files_payload.get("entries")
        if isinstance(entries, dict):
            out.extend([k for k in entries.keys() if isinstance(k, str) and k.strip()])
    return out


def _extract_resource_type_id(record: dict) -> str:
    metadata = record.get("metadata") or {}
    resource = metadata.get("resource_type") or {}
    if isinstance(resource, dict):
        rid = resource.get("id")
        if isinstance(rid, str):
            return rid
    return ""


def _extract_access_status(record: dict) -> str:
    access = record.get("access") or {}
    status = access.get("status")
    if isinstance(status, str):
        return status
    return ""


def _snapshot_from_record(record: dict) -> dict[str, str | list[str]]:
    metadata = record.get("metadata") or {}
    return {
        "title": strip_html(metadata.get("title") or record.get("title") or ""),
        "description": strip_html(metadata.get("description") or ""),
        "file_keys": _extract_file_keys(record.get("files") or {}),
        "resource_type_id": _extract_resource_type_id(record),
        "access_status": _extract_access_status(record),
    }


def load_cached_zenodo_records() -> dict[str, dict[str, str | list[str]]]:
    by_repo_doi: dict[str, dict[str, str | list[str]]] = {}
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
            by_repo_doi[repo_doi] = _snapshot_from_record(hit)
    return by_repo_doi


def fetch_zenodo_record(session: requests.Session, repo_doi: str) -> dict[str, str | list[str]] | None:
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
    return _snapshot_from_record(payload)


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


def _safe_cache_name(archive_key: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", archive_key).strip("_")[:120] or "archive"


def parse_preview_tree_filenames(html_text: str) -> list[str]:
    names: list[str] = []
    seen: set[str] = set()
    for raw in FILE_TEXT_RE.findall(html_text):
        name = html.unescape(raw).strip()
        if not name or name in seen:
            continue
        names.append(name)
        seen.add(name)
    return names


def fetch_preview_tree_filenames(
    session: requests.Session,
    record_id: str,
    archive_key: str,
    refresh: bool,
) -> list[str] | None:
    ZENODO_PREVIEW_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_path = ZENODO_PREVIEW_CACHE_DIR / f"{record_id}__{_safe_cache_name(archive_key)}.json"
    if cache_path.exists() and not refresh:
        try:
            payload = json.loads(cache_path.read_text(encoding="utf-8"))
            names = payload.get("file_names")
            if isinstance(names, list):
                return [n for n in names if isinstance(n, str)]
        except (OSError, json.JSONDecodeError):
            pass

    url = f"https://zenodo.org/records/{record_id}/preview/{quote(archive_key, safe='')}?include_deleted=0"
    try:
        resp = session.get(url, timeout=REQUEST_TIMEOUT)
    except requests.RequestException:
        return None
    if resp.status_code != 200:
        return None
    ctype = (resp.headers.get("Content-Type") or "").lower()
    if "html" not in ctype:
        return None
    names = parse_preview_tree_filenames(resp.text)
    if not names:
        return None
    try:
        cache_path.write_text(
            json.dumps(
                {
                    "record_id": record_id,
                    "archive_key": archive_key,
                    "fetched_at": int(time.time()),
                    "file_names": names,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
    except OSError:
        pass
    return names


def find_preview_tree_evidence(
    session: requests.Session,
    repo_doi: str,
    file_keys: list[str],
    resource_type_id: str,
    access_status: str,
    refresh: bool,
) -> str | None:
    record_id = record_id_from_repo_doi(repo_doi)
    if not record_id:
        return None

    archive_keys = [
        k for k in file_keys if Path(k).suffix.lower() in ARCHIVE_EXTENSIONS
    ]
    if not archive_keys:
        return None

    dataset_open = resource_type_id.lower() == "dataset" and access_status.lower() == "open"

    for archive_key in archive_keys[:2]:
        names = fetch_preview_tree_filenames(
            session=session,
            record_id=record_id,
            archive_key=archive_key,
            refresh=refresh,
        )
        if not names:
            continue

        lowered = [n.lower() for n in names]
        readmes = [n for n in names if README_RE.search(Path(n).name)]
        data_files = [
            n
            for n in names
            if (Path(n).suffix.lower() in DATA_EXTENSIONS)
            or any(h in n.lower() for h in DATA_NAME_HINTS)
        ]
        code_files = [
            n
            for n in names
            if (Path(n).suffix.lower() in CODE_EXTENSIONS)
            or any(h in n.lower() for h in CODE_NAME_HINTS)
        ]

        has_readme = bool(readmes)
        has_data = bool(data_files)
        has_code = bool(code_files)

        if has_readme and has_data and (has_code or dataset_open):
            top_examples = (readmes[:1] + data_files[:2] + code_files[:2])[:5]
            sample = "; ".join(top_examples)
            return (
                f"archive_preview:{archive_key} | readme={len(readmes)} data={len(data_files)} "
                f"code={len(code_files)} dataset_open={str(dataset_open).lower()} | sample: {sample}"
            )

        # If no clear evidence, continue with next archive if present.
        _ = lowered

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
        description="Conservative Zenodo metadata/tree upgrade for no-README repos"
    )
    parser.add_argument("--limit", type=int, default=None, help="Max repos to inspect")
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="Fetch fresh targeted metadata and preview trees (default uses caches when available)",
    )
    parser.add_argument("--dry-run", action="store_true", help="Report only; no DB writes")
    parser.add_argument(
        "--skip-preview-tree",
        action="store_true",
        help="Disable archive preview-tree fallback and use metadata text only",
    )
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
        session = build_session()

        scanned = upgraded = blocked_restriction = no_metadata = 0
        no_evidence = metadata_hits = preview_hits = 0
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

            title = str((meta.get("title") or "")).strip()
            description = str((meta.get("description") or "")).strip()
            file_keys = [k for k in (meta.get("file_keys") or []) if isinstance(k, str)]
            resource_type_id = str(meta.get("resource_type_id") or "")
            access_status = str(meta.get("access_status") or "")

            combined = "\n".join(x for x in (description, title) if x)
            if not combined and not file_keys:
                no_metadata += 1
                continue

            scanned += 1
            if has_restriction_signal(combined):
                blocked_restriction += 1
                continue

            snippet = find_explicit_full_data_evidence(description)
            evidence_source = "description"
            if not snippet:
                snippet = find_explicit_full_data_evidence(title)
                evidence_source = "title"

            if snippet:
                metadata_hits += 1
                final_source = f"{source_used}:{evidence_source}"
                final_snippet = snippet
            else:
                final_source = ""
                final_snippet = ""
                if not args.skip_preview_tree:
                    if idx > 1:
                        time.sleep(RATE_LIMIT_SLEEP)
                    tree_snippet = find_preview_tree_evidence(
                        session=session,
                        repo_doi=repo_doi,
                        file_keys=file_keys,
                        resource_type_id=resource_type_id,
                        access_status=access_status,
                        refresh=args.refresh,
                    )
                    if tree_snippet:
                        preview_hits += 1
                        final_source = f"{source_used}:preview_tree"
                        final_snippet = tree_snippet

            if not final_snippet:
                no_evidence += 1
                continue

            if len(sample_hits) < 10:
                sample_hits.append((repo_doi, final_source, final_snippet[:220]))

            if not args.dry_run:
                upsert_upgrade(conn, repo_doi, final_source, final_snippet)
            upgraded += 1

        LOGGER.info("=" * 60)
        LOGGER.info("Scanned metadata records: %d", scanned)
        LOGGER.info("Upgrades (all_data):      %d%s", upgraded, " [dry-run]" if args.dry_run else "")
        LOGGER.info("  via metadata text:      %d", metadata_hits)
        LOGGER.info("  via preview tree:       %d", preview_hits)
        LOGGER.info("Blocked by restrictions:  %d", blocked_restriction)
        LOGGER.info("No metadata text/files:   %d", no_metadata)
        LOGGER.info("No acceptable evidence:   %d", no_evidence)
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
