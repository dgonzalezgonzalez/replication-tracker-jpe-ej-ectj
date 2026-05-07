"""Ingest JPE-Reproducibility GitHub repos as replication evidence.

For each repository in the public org, the script pulls:
- `TEMPLATE.qmd` (replication report)
- `replication-package/replication/README*`

It extracts DOI candidates from those files, links repos to papers, and writes
classification evidence into `readme_analysis` using the shared classifier.

Usage:
    python scripts/06c_analyze_jpe_github.py
    python scripts/06c_analyze_jpe_github.py --limit 20
"""

from __future__ import annotations

import argparse
import base64
import importlib.util
import json
import logging
import os
import re
import sys
import time
import unicodedata
from difflib import SequenceMatcher
from pathlib import Path

import requests

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

try:
    from config import RAW_DIR
    from db import get_connection, init_db
except ImportError:  # pragma: no cover
    from scripts.config import RAW_DIR
    from scripts.db import get_connection, init_db

LOGGER = logging.getLogger(__name__)

ORG = "JPE-Reproducibility"
API = "https://api.github.com"
REQUEST_TIMEOUT = 30
RATE_LIMIT_SLEEP = 0.2
MAX_RETRIES = 5
BACKOFF_BASE_SECONDS = 1.0
DOI_RE = re.compile(r"10\.\d{4,9}/[-._;()/:a-z0-9]+", re.IGNORECASE)
DOI_URL_RE = re.compile(r"doi\.org/(10\.\d{4,9}/[-._;()/:a-z0-9]+)", re.IGNORECASE)
YEAR_FROM_REPO_RE = re.compile(r"-(20\d{2})\d{3,}$")
TITLE_LINE_RE = re.compile(r"^\s*title\s*:\s*[\"']?(.+?)[\"']?\s*$", re.IGNORECASE)
REPORT_DIR = RAW_DIR / "jpe_github"
TITLE_THRESHOLD = 0.94
AUTO_ACCEPT_THRESHOLD = 0.985
AUTO_ACCEPT_GAP = 0.02
OVERRIDES_PATH = RAW_DIR / "jpe_github_manual_overrides.json"


spec = importlib.util.spec_from_file_location(
    "classify_module", SCRIPT_DIR / "07_classify_readmes.py"
)
classify_module = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(classify_module)
classify_data_availability = classify_module.classify_data_availability


def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )


def build_session() -> requests.Session:
    s = requests.Session()
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "ReplicationTracker/1.0"}
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    s.headers.update(headers)
    return s


def get_json_with_retries(
    session: requests.Session, url: str, params: dict[str, object] | None = None
) -> dict | list | None:
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = session.get(url, params=params, timeout=REQUEST_TIMEOUT)
            if resp.status_code in {429, 500, 502, 503, 504}:
                resp.raise_for_status()
            resp.raise_for_status()
            return resp.json()
        except (requests.RequestException, ValueError) as exc:
            if attempt == MAX_RETRIES:
                LOGGER.warning("GitHub request failed: %s params=%s err=%s", url, params, exc)
                return None
            sleep_s = BACKOFF_BASE_SECONDS * (2 ** (attempt - 1))
            time.sleep(sleep_s)
    return None


def list_org_repos(session: requests.Session, limit: int) -> list[dict]:
    repos: list[dict] = []
    page = 1
    while True:
        payload = get_json_with_retries(
            session,
            f"{API}/orgs/{ORG}/repos",
            params={"type": "all", "per_page": 100, "page": page},
        )
        if payload is None:
            return repos
        batch = payload if isinstance(payload, list) else []
        if not batch:
            break
        repos.extend(batch)
        if limit > 0 and len(repos) >= limit:
            return repos[:limit]
        page += 1
        time.sleep(RATE_LIMIT_SLEEP)
    return repos


def is_paper_repo(repo_name: str) -> bool:
    return repo_name.lower().startswith("jpe-")


def extract_dois(text: str) -> list[str]:
    tl = text.lower()
    found = [m.group(0).rstrip(".,);]") for m in DOI_RE.finditer(tl)]
    found.extend(m.group(1).rstrip(".,);]") for m in DOI_URL_RE.finditer(tl))
    seen: set[str] = set()
    out: list[str] = []
    for doi in found:
        if doi not in seen:
            seen.add(doi)
            out.append(doi)
    return out


def get_repo_tree_paths(session: requests.Session, full_name: str, branch: str) -> list[str]:
    payload = get_json_with_retries(
        session,
        f"{API}/repos/{full_name}/git/trees/{branch}",
        params={"recursive": "1"},
    )
    if not isinstance(payload, dict):
        return []
    out: list[str] = []
    for node in payload.get("tree", []):
        if node.get("type") == "blob" and node.get("path"):
            out.append(node["path"])
    return out


def pick_evidence_paths(paths: list[str]) -> list[str]:
    selected: list[str] = []
    for p in paths:
        pl = p.lower()
        if pl.endswith(".qmd") and "template" in pl:
            selected.append(p)
            continue
        if pl.startswith("replication-package/") and "/readme" in pl:
            selected.append(p)
            continue
        if pl.endswith("/report-readme.md"):
            selected.append(p)
            continue
        if pl == "readme.md" or pl.startswith("readme"):
            selected.append(p)
    return selected


def normalize_title(text: str) -> str:
    t = unicodedata.normalize("NFKD", text)
    t = "".join(c for c in t if not unicodedata.combining(c))
    t = re.sub(r"[^\w\s]", " ", t)
    t = re.sub(r"\s+", " ", t).strip().lower()
    return t


def similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, a, b).ratio()


def repo_year_hint(repo_name: str) -> int | None:
    m = YEAR_FROM_REPO_RE.search(repo_name)
    if not m:
        return None
    try:
        return int(m.group(1))
    except ValueError:
        return None


def extract_candidate_titles(text: str) -> list[str]:
    out: list[str] = []
    for line in text.splitlines():
        m = TITLE_LINE_RE.match(line)
        if m:
            value = m.group(1).strip()
            if value and len(value) >= 20:
                out.append(value)
    for m in re.finditer(r"replication\s+(?:report|package)\s+for[:\s]+(.+)", text, re.IGNORECASE):
        value = m.group(1).strip().strip("\"'")
        if value and len(value) >= 20:
            out.append(value)
    seen: set[str] = set()
    dedup: list[str] = []
    for v in out:
        nv = normalize_title(v)
        if nv and nv not in seen:
            seen.add(nv)
            dedup.append(v)
    return dedup


def fetch_text_file(session: requests.Session, full_name: str, path: str, branch: str) -> str | None:
    payload = get_json_with_retries(
        session,
        f"{API}/repos/{full_name}/contents/{path}",
        params={"ref": branch},
    )
    if not isinstance(payload, dict):
        return None
    content = payload.get("content")
    encoding = payload.get("encoding")
    if not content or encoding != "base64":
        return None
    try:
        raw = base64.b64decode(content)
        return raw.decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001
        return None


def find_known_paper_doi(conn, dois: list[str]) -> str | None:
    for doi in dois:
        row = conn.execute(
            "SELECT doi FROM papers WHERE lower(doi)=? LIMIT 1", (doi,)
        ).fetchone()
        if row:
            return row["doi"]
    return None


def load_jpe_title_rows(conn) -> list[tuple[str, str, int | None]]:
    rows = conn.execute(
        """
        SELECT doi, title, publication_year
        FROM papers
        WHERE doi IS NOT NULL
          AND title IS NOT NULL
          AND journal_name LIKE 'Journal of Political Economy%'
        """
    ).fetchall()
    out: list[tuple[str, str, int | None]] = []
    for r in rows:
        out.append((r["doi"], normalize_title(r["title"]), r["publication_year"]))
    return out


def match_by_title(
    title_candidates: list[str],
    title_rows: list[tuple[str, str, int | None]],
    year_hint: int | None,
) -> tuple[str | None, float]:
    best_doi: str | None = None
    best_score = 0.0
    normalized_candidates = [normalize_title(t) for t in title_candidates if normalize_title(t)]
    for cand in normalized_candidates:
        for doi, ptitle, pyear in title_rows:
            if year_hint is not None and pyear is not None and abs(pyear - year_hint) > 2:
                continue
            score = similarity(cand, ptitle)
            if score > best_score:
                best_score = score
                best_doi = doi
    if best_score >= TITLE_THRESHOLD:
        return best_doi, best_score
    return None, best_score


def top_title_candidates(
    title_candidates: list[str],
    title_rows: list[tuple[str, str, int | None]],
    year_hint: int | None,
    limit: int = 3,
) -> list[tuple[str, float]]:
    normalized_candidates = [normalize_title(t) for t in title_candidates if normalize_title(t)]
    scores: dict[str, float] = {}
    for cand in normalized_candidates:
        for doi, ptitle, pyear in title_rows:
            if year_hint is not None and pyear is not None and abs(pyear - year_hint) > 2:
                continue
            score = similarity(cand, ptitle)
            prev = scores.get(doi, 0.0)
            if score > prev:
                scores[doi] = score
    ranked = sorted(scores.items(), key=lambda x: x[1], reverse=True)
    return ranked[:limit]


def should_auto_accept(cands: list[tuple[str, float]]) -> tuple[bool, str | None]:
    if not cands:
        return False, None
    top_doi, top_score = cands[0]
    second_score = cands[1][1] if len(cands) > 1 else 0.0
    if top_score >= AUTO_ACCEPT_THRESHOLD and (top_score - second_score) >= AUTO_ACCEPT_GAP:
        return True, top_doi
    return False, None


def load_overrides() -> dict[str, str]:
    if not OVERRIDES_PATH.exists():
        return {}
    try:
        payload = json.loads(OVERRIDES_PATH.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        LOGGER.warning("Could not parse overrides file: %s", OVERRIDES_PATH)
        return {}
    out: dict[str, str] = {}
    if isinstance(payload, dict):
        for k, v in payload.items():
            if isinstance(k, str) and isinstance(v, str):
                out[k.strip()] = v.strip()
    return out


def save_mapping_and_analysis(
    conn,
    paper_doi: str,
    repo_url: str,
    readme_text: str,
    classification: str | None,
    restriction_flags: list[str],
) -> None:
    with conn:
        conn.execute(
            """
            INSERT OR IGNORE INTO repo_mappings
                (paper_doi, repo_doi, icpsr_project_id, repo_host, source)
            VALUES (?, ?, NULL, 'github', 'jpe_github')
            """,
            (paper_doi, repo_url),
        )

        conn.execute(
            """
            INSERT INTO readme_analysis
                (repo_doi, repo_host, has_readme, readme_text,
                 restriction_flags, restriction_count, data_availability)
            VALUES (?, 'github', 1, ?, ?, ?, ?)
            ON CONFLICT(repo_doi) DO UPDATE SET
                repo_host=excluded.repo_host,
                has_readme=excluded.has_readme,
                readme_text=excluded.readme_text,
                restriction_flags=excluded.restriction_flags,
                restriction_count=excluded.restriction_count,
                data_availability=excluded.data_availability
            """,
            (
                repo_url,
                readme_text[:5000],
                json.dumps(restriction_flags),
                len(restriction_flags),
                classification,
            ),
        )


def main() -> int:
    configure_logging()
    parser = argparse.ArgumentParser(description="Analyze JPE GitHub replication repos")
    parser.add_argument("--limit", type=int, default=0, help="Process only first N repos")
    parser.add_argument(
        "--disable-auto-accept",
        action="store_true",
        help="Disable high-confidence automatic title-match acceptance",
    )
    args = parser.parse_args()

    conn = get_connection(init_db())
    session = build_session()

    stats = {
        "repos": 0,
        "with_evidence": 0,
        "with_doi": 0,
        "with_title_candidate": 0,
        "mapped_by_title": 0,
        "mapped_by_override": 0,
        "mapped_by_auto_accept": 0,
        "mapped": 0,
        "all_data": 0,
        "partial_data": 0,
        "no_data": 0,
        "unknown": 0,
    }
    unresolved: list[dict[str, object]] = []

    try:
        repos = list_org_repos(session, args.limit)
        LOGGER.info("JPE GitHub repos discovered: %d", len(repos))
        title_rows = load_jpe_title_rows(conn)
        overrides = load_overrides()
        LOGGER.info("Loaded manual overrides: %d (%s)", len(overrides), OVERRIDES_PATH)

        for repo in repos:
            stats["repos"] += 1
            repo_name = repo.get("name") or ""
            if not is_paper_repo(repo_name):
                continue
            full_name = repo.get("full_name")
            repo_url = repo.get("html_url")
            branch = repo.get("default_branch") or "main"
            if not full_name or not repo_url:
                continue

            paths = get_repo_tree_paths(session, full_name, branch)
            evidence_paths = pick_evidence_paths(paths)
            if not evidence_paths:
                continue

            snippets: list[str] = []
            for p in evidence_paths:
                text = fetch_text_file(session, full_name, p, branch)
                if text:
                    snippets.append(f"\n\n# FILE: {p}\n{text}")
                time.sleep(RATE_LIMIT_SLEEP)

            if not snippets:
                continue
            stats["with_evidence"] += 1

            combined = "\n".join(snippets)
            dois = extract_dois(combined)
            paper_doi = None
            if dois:
                stats["with_doi"] += 1
                paper_doi = find_known_paper_doi(conn, dois)

            best_score = 0.0
            if not paper_doi:
                override_doi = overrides.get(repo_name)
                if override_doi:
                    row = conn.execute(
                        "SELECT doi FROM papers WHERE lower(doi)=lower(?) LIMIT 1",
                        (override_doi,),
                    ).fetchone()
                    if row:
                        paper_doi = row["doi"]
                        stats["mapped_by_override"] += 1

            if not paper_doi:
                title_candidates = extract_candidate_titles(combined)
                if title_candidates:
                    stats["with_title_candidate"] += 1
                    paper_doi, best_score = match_by_title(
                        title_candidates, title_rows, repo_year_hint(repo_name)
                    )
                    if paper_doi:
                        stats["mapped_by_title"] += 1
                    elif not args.disable_auto_accept:
                        ranked = top_title_candidates(
                            title_candidates, title_rows, repo_year_hint(repo_name), limit=3
                        )
                        ok, auto_doi = should_auto_accept(ranked)
                        if ok and auto_doi:
                            paper_doi = auto_doi
                            best_score = ranked[0][1]
                            stats["mapped_by_auto_accept"] += 1

            if not paper_doi:
                ranked = []
                title_candidates = extract_candidate_titles(combined)
                if title_candidates:
                    ranked = top_title_candidates(
                        title_candidates, title_rows, repo_year_hint(repo_name), limit=3
                    )
                unresolved.append(
                    {
                        "repo_name": repo_name,
                        "repo_url": repo_url,
                        "year_hint": repo_year_hint(repo_name),
                        "doi_candidates": dois[:10],
                        "evidence_paths": evidence_paths[:20],
                        "best_title_score": round(best_score, 4),
                        "top_title_candidates": [
                            {"doi": doi, "score": round(score, 4)}
                            for doi, score in ranked
                        ],
                    }
                )
                continue

            cls, flags = classify_data_availability(combined)
            if cls == "all_data":
                stats["all_data"] += 1
            elif cls == "partial_data":
                stats["partial_data"] += 1
            elif cls == "no_data":
                stats["no_data"] += 1
            else:
                stats["unknown"] += 1

            save_mapping_and_analysis(conn, paper_doi, repo_url, combined, cls, flags)
            stats["mapped"] += 1

        REPORT_DIR.mkdir(parents=True, exist_ok=True)
        (REPORT_DIR / "unresolved_repos.json").write_text(
            json.dumps(unresolved, indent=2),
            encoding="utf-8",
        )
        LOGGER.info("JPE GitHub ingest summary: %s", stats)
        LOGGER.info(
            "Unresolved repos report: %s (%d repos)",
            REPORT_DIR / "unresolved_repos.json",
            len(unresolved),
        )
        return 0
    except KeyboardInterrupt:
        LOGGER.info("Interrupted")
        return 130
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
