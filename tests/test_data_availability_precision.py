from __future__ import annotations

import importlib.util
from pathlib import Path


def _load_classifier():
    root = Path(__file__).resolve().parents[1]
    script = root / "scripts" / "07_classify_readmes.py"
    spec = importlib.util.spec_from_file_location("classify_module", script)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module.classify_data_availability


classify_data_availability = _load_classifier()


def test_explicit_all_data_phrase_returns_all_data():
    text = "All data and code are provided in this repository."
    cls, flags = classify_data_availability(text)
    assert cls == "all_data"
    assert flags == []


def test_restriction_blocks_all_data():
    text = (
        "All data are publicly available, but proprietary vendor files are "
        "available upon request."
    )
    cls, _flags = classify_data_availability(text)
    assert cls == "partial_data"


def test_ambiguous_text_is_not_promoted_to_all_data():
    text = "Replication package includes scripts and notes."
    cls, flags = classify_data_availability(text)
    assert cls is None
    assert flags == []


def test_open_repository_link_plus_clear_phrase_counts_as_all_data():
    text = (
        "Data and code are available at https://zenodo.org/record/1234567 "
        "for replication."
    )
    cls, flags = classify_data_availability(text)
    assert cls == "all_data"
    assert flags == []
