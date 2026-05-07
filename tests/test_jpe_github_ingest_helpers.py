from __future__ import annotations

import importlib.util
from pathlib import Path


def _load_module():
    root = Path(__file__).resolve().parents[1]
    script = root / "scripts" / "06c_analyze_jpe_github.py"
    spec = importlib.util.spec_from_file_location("jpe_github_module", script)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


module = _load_module()


def test_extract_dois_finds_and_normalizes():
    text = "Paper DOI: 10.1086/741641. Backup mention 10.1086/741641)"
    out = module.extract_dois(text)
    assert out == ["10.1086/741641"]


def test_pick_evidence_paths_selects_template_and_replication_readme():
    paths = [
        "src/code/run.do",
        "TEMPLATE.qmd",
        "replication-package/replication/README.md",
        "replication-package/notes.txt",
    ]
    out = module.pick_evidence_paths(paths)
    assert "TEMPLATE.qmd" in out
    assert "replication-package/replication/README.md" in out
    assert "src/code/run.do" not in out


def test_is_paper_repo_filters_tooling_repos():
    assert module.is_paper_repo("JPE-Richert-20240555")
    assert not module.is_paper_repo("JPEtools.jl")


def test_repo_year_hint_extracts_year():
    assert module.repo_year_hint("JPE-Richert-20240555") == 2024
    assert module.repo_year_hint("JPE-template") is None


def test_extract_candidate_titles_reads_yaml_title():
    text = 'title: "Commuting for crime"\nother: x'
    out = module.extract_candidate_titles(text)
    assert out == []
    text2 = 'title: "Long enough paper title for matching in tests"'
    out2 = module.extract_candidate_titles(text2)
    assert len(out2) == 1


def test_match_by_title_strict_threshold():
    candidates = ["A test paper title with enough words"]
    rows = [("10.1/x", module.normalize_title("A test paper title with enough words"), 2024)]
    doi, score = module.match_by_title(candidates, rows, 2024)
    assert doi == "10.1/x"
    assert score >= 0.99


def test_should_auto_accept_requires_large_gap_and_high_score():
    ok, doi = module.should_auto_accept([("10.1/a", 0.99), ("10.1/b", 0.95)])
    assert ok
    assert doi == "10.1/a"

    ok2, doi2 = module.should_auto_accept([("10.1/a", 0.99), ("10.1/b", 0.98)])
    assert not ok2
    assert doi2 is None
