"""Run the JPE/EJ/ECTJ replication-tracker data pipeline."""

from __future__ import annotations

import argparse
import logging
import shutil
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

try:
    from config import DB_PATH, RAW_DIR
except ImportError:  # pragma: no cover
    from scripts.config import DB_PATH, RAW_DIR

LOGGER = logging.getLogger(__name__)


def configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )


def run_step(label: str, cmd: list[str]) -> None:
    LOGGER.info("=" * 60)
    LOGGER.info("%s", label)
    LOGGER.info("$ %s", " ".join(cmd))
    completed = subprocess.run(cmd, cwd=PROJECT_ROOT)
    if completed.returncode != 0:
        raise RuntimeError(f"Step failed ({label}) with exit code {completed.returncode}")


def reset_state() -> None:
    if DB_PATH.exists():
        DB_PATH.unlink()
        LOGGER.info("Removed DB: %s", DB_PATH)

    pipeline_state = PROJECT_ROOT / "data" / "pipeline_state.json"
    if pipeline_state.exists():
        pipeline_state.unlink()
        LOGGER.info("Removed pipeline state: %s", pipeline_state)

    for child in [
        RAW_DIR / "journals",
        RAW_DIR / "papers",
        RAW_DIR / "jpe_dataverse",
        RAW_DIR / "zenodo_communities",
        RAW_DIR / "external_repos",
    ]:
        if child.exists():
            shutil.rmtree(child)
            LOGGER.info("Removed raw cache: %s", child)


def main() -> int:
    configure_logging()

    parser = argparse.ArgumentParser(description="Run JPE/EJ/ECTJ pipeline")
    parser.add_argument(
        "--no-reset",
        action="store_true",
        help="Do not wipe DB/cache before running",
    )
    parser.add_argument(
        "--skip-frontend-build",
        action="store_true",
        help="Skip npm build and docs sync",
    )
    args = parser.parse_args()

    try:
        if not args.no_reset:
            reset_state()

        run_step("Fetch journals", [sys.executable, "scripts/01_fetch_journals.py"])
        run_step("Fetch papers", [sys.executable, "scripts/02_fetch_papers.py", "--full"])
        run_step("Map JPE Dataverse", [sys.executable, "scripts/03e_jpe_dataverse_mapping.py"])
        run_step("Map EJ/ECTJ Zenodo", [sys.executable, "scripts/03f_zenodo_community_mapping.py"])
        run_step(
            "Analyze external repos",
            [sys.executable, "scripts/06b_analyze_external_repos.py", "--hosts", "dataverse,zenodo"],
        )
        run_step("Classify README texts", [sys.executable, "scripts/09a_reclassify_readmes.py"])
        run_step("Compute scores", [sys.executable, "scripts/09_compute_scores.py"])
        run_step("Export static data", [sys.executable, "scripts/export_static_data.py"])

        if not args.skip_frontend_build:
            run_step("Install frontend deps", ["npm", "install", "--prefix", "frontend"])
            run_step("Build frontend", ["npm", "run", "build:pages", "--prefix", "frontend"])
            docs_dir = PROJECT_ROOT / "docs"
            if docs_dir.exists():
                shutil.rmtree(docs_dir)
            shutil.copytree(PROJECT_ROOT / "frontend" / "dist", docs_dir)
            LOGGER.info("Synced docs/ from frontend/dist")

        LOGGER.info("=" * 60)
        LOGGER.info("Pipeline finished successfully")
        return 0
    except Exception as exc:  # noqa: BLE001
        LOGGER.error("Pipeline failed: %s", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
