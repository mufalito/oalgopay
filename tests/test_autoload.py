"""Tests for the opt-in wallet auto-loader helpers (watcher)."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.watcher import AUTOLOAD_RETRY_S, _autoload_due, _core_wallet_file  # noqa: E402


class TestCoreWalletFile(unittest.TestCase):
    def test_reads_wallet_file_from_core_settings(self):
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "settings.json").write_text('{"wallet_file": "/wallets/shop.wallet.dat"}')
            self.assertEqual(_core_wallet_file(td), "/wallets/shop.wallet.dat")

    def test_missing_settings_returns_none(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertIsNone(_core_wallet_file(td))

    def test_invalid_json_returns_none(self):
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "settings.json").write_text("{not json")
            self.assertIsNone(_core_wallet_file(td))

    def test_blank_wallet_file_returns_none(self):
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "settings.json").write_text('{"wallet_file": "  "}')
            self.assertIsNone(_core_wallet_file(td))


class TestAutoloadDue(unittest.TestCase):
    def test_first_attempt_is_due(self):
        self.assertTrue(_autoload_due(SimpleNamespace(), 1000.0))

    def test_throttled_within_window(self):
        state = SimpleNamespace(wallet_autoload_at=1000.0)
        self.assertFalse(_autoload_due(state, 1000.0 + AUTOLOAD_RETRY_S - 1))

    def test_due_again_after_window(self):
        state = SimpleNamespace(wallet_autoload_at=1000.0)
        self.assertTrue(_autoload_due(state, 1000.0 + AUTOLOAD_RETRY_S))


if __name__ == "__main__":
    unittest.main()
