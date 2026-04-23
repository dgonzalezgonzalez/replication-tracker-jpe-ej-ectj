# Zenodo No-README Metadata Upgrade Requirements

Date: 2026-04-23
Status: Brainstorm complete
Owner: Data pipeline

## Problem
Many papers are currently labeled `unanalyzed_repo` because the README is inside a packaged archive (usually `.zip`) and is not visible via top-level file listing. For Zenodo, repository metadata descriptions often contain explicit evidence that replication materials include data/code/tables, but this signal is currently unused.

## Goal
Reduce false `unanalyzed_repo` counts by upgrading a subset of Zenodo no-README cases to `full_data` using highly explicit metadata evidence, with strict false-positive control.

## Scope
In scope:
- Re-evaluate only Zenodo repos currently without usable README text.
- Use Zenodo `metadata.description` and `metadata.title` as evidence sources.
- Allow only `unanalyzed_repo -> full_data` upgrades.
- Persist audit evidence in DB fields.
- Run via a separate script (manual invocation).

Out of scope:
- Dataverse metadata-based upgrades.
- Assigning `partial_data` or `no_data` from metadata.
- Full-archive download/unzip for general packages.
- Changing dashboard category taxonomy.

## Product Decisions (Locked)
- Host: Zenodo only.
- Category output: keep existing categories; upgraded items become `full_data`.
- Safety policy: minimize false positives.
- Rule strictness: very strict.
- Evidence sources: `description + title` only.
- Persistence: DB fields in `readme_analysis` for metadata evidence.
- Execution mode: separate script.
- Metadata source mode: cached files by default, optional `--refresh` to query targeted records.

## Functional Requirements
1. Target selection
- Script must process only repos that are:
  - mapped to `repo_host='zenodo'`, and
  - currently no-README/unusable README state (`has_readme=0` or empty `readme_text`), and
  - linked to papers currently scoring as `unanalyzed_repo`.

2. Evidence extraction
- For each target repo, derive evidence text from Zenodo metadata fields:
  - `metadata.description`
  - `metadata.title`
- Default source is cached metadata under `data/raw/zenodo_communities/*.json`.
- Optional `--refresh` flag may fetch fresh metadata for target repo IDs.

3. Conservative upgrade rule (very strict)
- Upgrade to `full_data` only when text contains:
  - explicit inclusion verb (e.g., includes/contains/provides), and
  - explicit data artifact mention, and
  - at least one additional reproducibility artifact (e.g., code or tables/figures/results).
- If any restriction signal appears (confidential/restricted/DUA/proprietary/not publicly available/etc.), no upgrade.
- If evidence is ambiguous, keep unchanged.

4. Writeback behavior
- For upgraded repos, update `readme_analysis.data_availability` to `all_data`.
- Store provenance in new `readme_analysis` fields:
  - `metadata_evidence_source`
  - `metadata_evidence_snippet`
  - `metadata_upgraded_at`
- Do not overwrite real README text fields.

5. Recompute scores
- After script run, standard score recomputation must yield paper-level status updates via existing scoring logic.

## Non-Functional Requirements
- Fast execution: process only targeted no-README Zenodo subset.
- Idempotent behavior: repeated runs should not create inconsistent state.
- Auditable: every upgrade must have stored evidence snippet/source.
- Safe by default: no broad network crawling unless explicitly requested.

## Success Criteria
- Primary: meaningful reduction in `unanalyzed_repo` count for Zenodo-backed papers.
- Quality: sampled upgraded cases show clear explicit evidence and low false-positive rate.
- Transparency: each upgraded repo has persisted metadata evidence in DB.

## Validation
- Before/after counts:
  - number of `unanalyzed_repo` papers
  - number of `full_data` papers
  - number of upgraded Zenodo repos
- Manual spot-check sample of upgraded repos against stored evidence snippet.
- Confirm no changes were made to non-Zenodo repos.

## Risks
- Overly optimistic metadata phrasing may still mislead classification.
- Cached metadata staleness when not using `--refresh`.
- Phrase rules may miss valid full-data cases (false negatives), accepted for safety.

## Rollout Plan
1. Add DB migration for metadata evidence fields.
2. Add standalone script for Zenodo metadata conservative upgrades.
3. Run script on current DB (no full pipeline rerun).
4. Recompute scores.
5. Review before/after metrics and sample evidence.

## Open Items
- Threshold for manual review gate (e.g., require human approval if upgrades exceed X% in a run).
