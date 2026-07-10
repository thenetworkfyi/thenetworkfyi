from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch


def test_enabled_scanner_error_fails_closed(monkeypatch):
    from thenetwork.security import content_scan

    monkeypatch.setattr(content_scan, "_scanner", None)
    with patch("thenetwork.security.content_scan.get_settings", return_value=SimpleNamespace(content_scan_enabled=True)), patch(
        "thenetwork.security.content_scan._get_scanner", side_effect=RuntimeError("model unavailable")
    ):
        assert content_scan.scan_content("hello") == (False, "scanner_error")
