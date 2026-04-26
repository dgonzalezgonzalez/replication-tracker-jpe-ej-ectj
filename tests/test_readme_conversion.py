from __future__ import annotations

from unittest.mock import patch

from scripts.readme_conversion import extract_text_from_bytes


@patch("scripts.readme_conversion._extract_with_markitdown", return_value=None)
def test_plain_text_fallback_decodes_utf8(_mock_markitdown):
    text = extract_text_from_bytes(b"hello\nworld\n", "README.txt")
    assert text == "hello\nworld"


@patch("scripts.readme_conversion._extract_with_markitdown", return_value=None)
def test_unknown_extension_returns_none(_mock_markitdown):
    text = extract_text_from_bytes(b"\x00\x01\x02", "README.unknown")
    assert text is None
