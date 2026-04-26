# Economics Replication Tracker (JPE, EJ, ECTJ)

Local-first replication tracker for:
- Journal of Political Economy
- Journal of Political Economy Macroeconomics
- Journal of Political Economy Microeconomics
- The Economic Journal
- Econometrics Journal

## What it does

- Pulls paper metadata from OpenAlex.
- Maps papers to replication repositories from:
  - Harvard Dataverse (`JPE` dataverse)
  - Zenodo communities (`ej-replication-repository`, `ectj-replication-repository`)
- Downloads repository file listings and README text (Dataverse/Zenodo APIs).
- Converts weird-format README/docs using Microsoft MarkItDown plus fallback parsers.
- Classifies data availability with precision-first guardrails (`full_data` assigned only on strong evidence).
- Computes paper-level replication status and exports static dashboard JSON.

## Environment setup

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

`requirements.txt` installs MarkItDown directly from Microsoft repo:

```text
markitdown @ git+https://github.com/microsoft/markitdown.git#subdirectory=packages/markitdown
```

## Run end-to-end

```bash
python3 scripts/run_pipeline_journals.py
```

This generates:
- `frontend/public/data/*.json`
- `docs/index.html` (GitHub Pages build)
- `docs/index-local.html` (double-click local HTML file; no local server needed)

## Open dashboard locally (double-click)

After running the pipeline, open:

- `docs/index-local.html`

This file embeds all dashboard JSON data and works via `file://` (double-click).

Note: the dashboard view intentionally filters to papers from 2016 onward.

## Optional flags

```bash
python3 scripts/run_pipeline_journals.py --no-reset
python3 scripts/run_pipeline_journals.py --skip-frontend-build
```

## Conservative Zenodo metadata/tree upgrade (no-README subset)

To reduce false `unanalyzed_repo` only for Zenodo repos that currently have no usable README:

```bash
python3 scripts/09b_upgrade_zenodo_metadata.py --dry-run
python3 scripts/09b_upgrade_zenodo_metadata.py
```

Optional:

```bash
python3 scripts/09b_upgrade_zenodo_metadata.py --refresh
python3 scripts/09b_upgrade_zenodo_metadata.py --limit 100 --dry-run
```

Rules are conservative by design:
- host restricted to Zenodo
- primary source is metadata title/description
- fallback source is Zenodo archive preview tree (without full archive download)
- only upgrades to `full_data` (`all_data` internally)
- any restriction signal blocks upgrade

Preview-tree fallback upgrades only when archive contents show:
- README-like file
- data-like files (e.g., `.dta`, `.csv`, `.xlsx`)
- code/repro artifact files (e.g., `.do`, `.py`, `.r`) or dataset+open status

Optional:

```bash
python3 scripts/09b_upgrade_zenodo_metadata.py --skip-preview-tree
python3 scripts/09b_upgrade_zenodo_metadata.py --refresh --limit 200
```
