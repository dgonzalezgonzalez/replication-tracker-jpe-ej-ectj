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
- Classifies data availability (`full_data`, `partial_data`, `no_data`).
- Computes paper-level replication status and exports static dashboard JSON.

## Run end-to-end

```bash
python3 scripts/run_pipeline_journals.py
```

This generates:
- `frontend/public/data/*.json`
- `docs/index.html` (static dashboard build)

## Optional flags

```bash
python3 scripts/run_pipeline_journals.py --no-reset
python3 scripts/run_pipeline_journals.py --skip-frontend-build
```
