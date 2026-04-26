---
title: "feat: Integrate MarkItDown for README extraction precision"
type: feat
status: active
date: 2026-04-26
---

# feat: Integrate MarkItDown for README extraction precision

## Overview

Install Microsoft MarkItDown and route README/document extraction through a shared converter layer so more odd formats are parsed reliably, then tighten classification guardrails so `full_data` is assigned only with strong positive evidence.

## Problem Frame

Current extraction is split across scripts and relies on per-format handlers (`fitz`, `openpyxl`, ad-hoc decoders). Some README-like files are missed or poorly extracted, which leaves false negatives (`unanalyzed_repo`) and can also produce false positives where weak text is interpreted as `all_data` and becomes `full_data`.

User goal is precision-first: if status says `full_data`, confidence should be as high as possible.

## Requirements Trace

- R1. Install MarkItDown from `https://github.com/microsoft/markitdown.git` in this project workflow.
- R2. Use MarkItDown to improve conversion of weird-formatted README-like files to markdown/text.
- R3. Reduce false positives in `full_data` classification (precision over recall).
- R4. Keep pipeline outputs and dashboard contracts stable (`replication_status` categories unchanged).
- R5. Land implementation and push changes to remote.

## Scope Boundaries

- No change to dashboard schema or status enum names.
- No large redesign of repository discovery/mapping stages (`01`-`05` scripts).
- No replacement of existing manual extractors for every format in one pass; MarkItDown is primary path with controlled fallback.

## Context & Research

### Relevant Code and Patterns

- `scripts/06b_analyze_external_repos.py`: primary active README extraction for Dataverse/Zenodo/Mendeley.
- `scripts/06_analyze_repos.py`: openICPSR extraction path with similar logic.
- `scripts/07_classify_readmes.py`: current classification rules and text extraction fallback used by `08`/`09a`.
- `scripts/08_deep_readme_search.py`: reuses extraction/classification from `07`.
- `scripts/09a_reclassify_readmes.py`: re-runs classification on stored texts.
- `scripts/09_compute_scores.py`: maps `all_data` -> `full_data`.
- `scripts/run_pipeline_journals.py`: orchestration entrypoint.
- `requirements.txt`: dependency management point.

### Institutional Learnings

- No `docs/solutions/` directory found in this repository; no local learnings artifact to carry forward.

### External References

- MarkItDown repo and README (`github.com/microsoft/markitdown`): install path, optional extras, security caveat, API/CLI usage.
- PyPI `markitdown` package: current published version and install options.

## Key Technical Decisions

- **Use MarkItDown Python API via shared helper module**: centralizes conversion behavior and removes duplicated extractor code in `06`, `06b`, and `07`.
- **Bias to conservative classification**: require explicit positive evidence for `all_data`; ambiguous extraction should not default to `all_data`.
- **Keep DB/API contract unchanged**: all precision improvements happen before `data_availability` and status derivation.
- **Add extraction provenance metadata in logs (not schema expansion)**: track when MarkItDown path succeeds/fails without forcing migration.

## Open Questions

### Resolved During Planning

- **Install source vs PyPI?** Use repo source installation as requested (`git clone` + editable install) and pin a compatible dependency entry so CI/local reproducibility is controlled.
- **Where to integrate first?** Start in `06b` (main pipeline path), then unify `06` and `07` to prevent drift.

### Deferred to Implementation

- **Exact pin strategy in `requirements.txt`** (`git+https` direct ref vs versioned package) after checking environment compatibility and install speed.
- **Whether to persist extraction source in DB** (new column) vs keep log-only provenance; finalize after evaluating migration cost.

## Implementation Units

- [ ] **Unit 1: Add MarkItDown dependency and shared conversion utility**

**Goal:** Introduce one reusable converter that wraps MarkItDown and safe fallbacks.

**Requirements:** R1, R2

**Dependencies:** None

**Files:**
- Modify: `requirements.txt`
- Create: `scripts/readme_conversion.py`
- Test: `tests/test_readme_conversion.py`

**Approach:**
- Add MarkItDown dependency (source-compatible install target) and define a utility API like `extract_text(raw_bytes, filename)` and `extract_text_from_path(path)`.
- Prefer MarkItDown conversion first for supported formats; fallback to existing parser-specific logic only when conversion errors or empty text.
- Normalize output (strip binary noise, cap extreme length similarly to current 5000-char DB write cap policy handled downstream).

**Patterns to follow:**
- Existing extractor signatures in `scripts/06b_analyze_external_repos.py` and `scripts/07_classify_readmes.py`.

**Test scenarios:**
- Happy path: DOCX bytes containing table + paragraph convert to non-empty markdown text.
- Happy path: PDF bytes convert to non-empty text.
- Edge case: unknown extension returns `None` without crash.
- Error path: malformed binary raises internal exception but helper returns `None` and logs warning.
- Integration: helper output remains plain text usable by existing classifier (no binary artifacts).

**Verification:**
- Shared helper can be imported from all pipeline scripts and returns deterministic text/`None` across representative formats.

- [ ] **Unit 2: Wire shared converter into repo analyzers**

**Goal:** Remove duplicated extraction logic and ensure all README ingestion paths use same conversion behavior.

**Requirements:** R2, R4

**Dependencies:** Unit 1

**Files:**
- Modify: `scripts/06b_analyze_external_repos.py`
- Modify: `scripts/06_analyze_repos.py`
- Modify: `scripts/07_classify_readmes.py`
- Modify: `scripts/08_deep_readme_search.py`
- Test: `tests/test_readme_ingestion_paths.py`

**Approach:**
- Replace local `extract_readme_text` implementations with imports from `scripts/readme_conversion.py`.
- Keep call sites and DB writes intact to avoid interface churn.
- Preserve existing cache behavior (`data/raw/...`) and retry flows.

**Execution note:** Start with characterization tests for current DB-write behavior (`has_readme`, `readme_text` nullability), then swap extractor wiring.

**Patterns to follow:**
- Existing repo processing loops and `insert_readme`/`save_result` functions in each script.

**Test scenarios:**
- Happy path: `06b` flow with README bytes stores extracted text and `has_readme=1`.
- Edge case: conversion returns empty/None; row still saved with `has_readme=1`, `readme_text=NULL`.
- Error path: converter exception does not stop loop; repo continues and pipeline stats increment appropriately.
- Integration: `08_deep_readme_search.py` still reuses classification path after extractor migration.

**Verification:**
- No script-level extractor duplication remains; ingestion scripts run with identical external behavior except improved extraction coverage.

- [ ] **Unit 3: Tighten full-data classification guardrails**

**Goal:** Reduce false positives where weak/ambiguous text becomes `all_data`.

**Requirements:** R3, R4

**Dependencies:** Unit 2

**Files:**
- Modify: `scripts/07_classify_readmes.py`
- Modify: `scripts/09a_reclassify_readmes.py`
- Modify: `scripts/09_compute_scores.py` (only if needed for conservative mapping safeguards)
- Test: `tests/test_data_availability_precision.py`

**Approach:**
- Introduce explicit-positive-evidence gate for `all_data` (e.g., strong include/provided statements) and downgrade ambiguous cases to `partial_data` or leave unclassified based on rule set.
- Keep restriction phrase handling conservative: any restriction indicator should block `all_data`.
- Reclassify existing stored README texts using `09a` after rules update to align historical records.

**Patterns to follow:**
- Existing phrase-list and table parsing architecture in `07`.
- Existing derive-status mapping in `09_compute_scores.py`.

**Test scenarios:**
- Happy path: README explicitly says all replication data included in package -> `all_data`.
- Edge case: README says data available externally (Zenodo/Dataverse link) with no restriction language -> `all_data` only when wording is explicit.
- Error path: ambiguous README with no explicit evidence -> not `all_data`.
- Error path: README contains both all-data language and one restriction phrase -> `partial_data` (never `all_data`).
- Integration: `all_data` still maps to `full_data`; downgraded cases reduce `full_data` count in controlled test fixture.

**Verification:**
- Precision-focused fixtures show zero false-positive `all_data` for known ambiguous/restricted samples.

- [ ] **Unit 4: Pipeline integration, docs, and operational safety**

**Goal:** Make upgraded conversion/classification path runnable end-to-end and understandable for maintainers.

**Requirements:** R1, R2, R3, R4, R5

**Dependencies:** Units 1-3

**Files:**
- Modify: `scripts/run_pipeline_journals.py`
- Modify: `README.md`
- Test: `tests/test_pipeline_markitdown_integration.py`

**Approach:**
- Ensure pipeline step ordering supports updated extraction + reclassification without manual intervention.
- Document dependency install and precision-first behavior changes in README.
- Add regression fixture run (small deterministic set) to validate no status-schema break and expected precision shift.

**Patterns to follow:**
- Current orchestration style in `scripts/run_pipeline_journals.py`.
- Existing README sections for optional upgrades and conservative rules.

**Test scenarios:**
- Happy path: pipeline run on fixture dataset completes and exports JSON artifacts.
- Edge case: MarkItDown unavailable in environment triggers clear failure or fallback path per design (deterministic behavior).
- Integration: exported `replication_status` values remain within allowed enum set.
- Integration: known ambiguous fixture no longer labeled `full_data`.

**Verification:**
- End-to-end fixture run produces stable artifact schema and improved precision metrics.

## System-Wide Impact

- **Interaction graph:** `06/06b/07/08 -> readme_analysis -> 09a -> 09_compute_scores -> export/API/dashboard`.
- **Error propagation:** conversion failures must degrade to null text and conservative classification, not hard-stop broad batches.
- **State lifecycle risks:** reclassification can shift aggregate counts; must be intentional and documented.
- **API surface parity:** API and frontend consume same status fields; no contract change allowed.
- **Integration coverage:** verify ingestion + reclassification + score derivation together, not only isolated classifier tests.
- **Unchanged invariants:** status enum and JSON export field names remain unchanged.

## Risks & Dependencies

| Risk | Mitigation |
|------|------------|
| MarkItDown dependency or extras increase install fragility | Pin tested install method and keep fallback extractors for critical formats |
| Over-conservative rules reduce true `full_data` recall too much | Use curated fixture set with known labels and monitor precision/recall deltas |
| Reclassification changes historical trend charts | Document expected shift and keep reproducible before/after summaries |
| Large/binary documents cause converter instability | Enforce size/type guardrails and exception-safe fallbacks |

## Documentation / Operational Notes

- Update README with MarkItDown install path (source repo) and pipeline expectations.
- Add brief maintainer note describing why precision is prioritized over recall for `full_data`.
- Include rerun guidance for reclassification on existing DBs.

## Sources & References

- Related code: `scripts/06b_analyze_external_repos.py`, `scripts/07_classify_readmes.py`, `scripts/09_compute_scores.py`, `scripts/run_pipeline_journals.py`
- External docs: `https://github.com/microsoft/markitdown`, `https://pypi.org/project/markitdown/`
