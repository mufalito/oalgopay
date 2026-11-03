"""Tests for the wallet sign-in verification API: verdict classification,
challenge lifecycle, and per-IP lockout bookkeeping."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db  # noqa: E402
from app.api.signin import CHALLENGE_TTL_S, LOCKOUT_WINDOW_S, classify_verify  # noqa: E402
from app.blocknet import BlocknetError  # noqa: E402

NOW = 1_700_000_000


def setUpModule():
    db.connect(":memory:")  # schema auto-creates


class TestClassifyVerify(unittest.TestCase):
    def test_true_is_valid(self):
        self.assertEqual(classify_verify(True), "valid")

    def test_false_is_invalid(self):
        self.assertEqual(classify_verify(False), "invalid")

    def test_node_400_is_definite_invalid(self):
        self.assertEqual(classify_verify(BlocknetError("invalid signature: must be 64 bytes hex-encoded", "error", 400)), "invalid")

    def test_node_429_is_infra(self):
        self.assertEqual(classify_verify(BlocknetError("rate limited", "error", 429)), "infra")

    def test_node_5xx_is_infra(self):
        self.assertEqual(classify_verify(BlocknetError("boom", "error", 500)), "infra")
        self.assertEqual(classify_verify(BlocknetError("boom", "error", 503)), "infra")

    def test_unreachable_is_infra(self):
        self.assertEqual(classify_verify(BlocknetError("daemon unreachable: …", "unreachable", None)), "infra")

    def test_auth_error_is_infra(self):
        self.assertEqual(classify_verify(BlocknetError("cookie unreadable", "auth", None)), "infra")


class TestChallengeLifecycle(unittest.TestCase):
    def test_create_then_get_returns_fresh_row(self):
        db.create_auth_challenge("nonce-a", NOW)
        row = db.get_auth_challenge("nonce-a")
        self.assertIsNotNone(row)
        self.assertEqual(row["nonce"], "nonce-a")
        self.assertEqual(row["created_at"], NOW)
        self.assertIsNone(row["used_at"])

    def test_consume_is_single_use(self):
        db.create_auth_challenge("nonce-b", NOW)
        self.assertTrue(db.consume_auth_challenge("nonce-b", NOW + 1))
        self.assertFalse(db.consume_auth_challenge("nonce-b", NOW + 2))

    def test_consume_missing_challenge_is_false(self):
        self.assertFalse(db.consume_auth_challenge("never-minted", NOW))

    def test_sweep_removes_used_expired_and_stale_keeps_fresh(self):
        db.create_auth_challenge("used", NOW)
        db.consume_auth_challenge("used", NOW + 5)
        db.create_auth_challenge("expired", NOW - CHALLENGE_TTL_S - 1)
        db.create_auth_challenge("fresh", NOW)
        db.record_auth_failure("1.2.3.4", NOW - LOCKOUT_WINDOW_S - 1)
        db.record_auth_failure("1.2.3.4", NOW)

        db.sweep_auth_challenges(NOW, CHALLENGE_TTL_S, LOCKOUT_WINDOW_S)

        self.assertIsNone(db.get_auth_challenge("used"))
        self.assertIsNone(db.get_auth_challenge("expired"))
        row = db.get_auth_challenge("fresh")
        self.assertIsNotNone(row)
        self.assertIsNone(row["used_at"])
        # only the in-window attempt survived
        self.assertEqual(db.count_recent_auth_failures("1.2.3.4", NOW - LOCKOUT_WINDOW_S), 1)


class TestLockout(unittest.TestCase):
    IP = "203.0.113.9"

    def setUp(self):
        db.clear_auth_failures(self.IP)

    def test_five_recent_failures_counted_then_cleared(self):
        for i in range(5):
            db.record_auth_failure(self.IP, NOW + i)
        self.assertEqual(db.count_recent_auth_failures(self.IP, NOW - LOCKOUT_WINDOW_S), 5)
        db.clear_auth_failures(self.IP)
        self.assertEqual(db.count_recent_auth_failures(self.IP, NOW - LOCKOUT_WINDOW_S), 0)

    def test_old_failures_deleted_by_sweep(self):
        for i in range(5):
            db.record_auth_failure(self.IP, NOW - LOCKOUT_WINDOW_S - 100 + i)
        # outside the window: not counted even before sweep
        self.assertEqual(db.count_recent_auth_failures(self.IP, NOW - LOCKOUT_WINDOW_S), 0)
        db.sweep_auth_challenges(NOW, CHALLENGE_TTL_S, LOCKOUT_WINDOW_S)
        # and gone entirely once a window that would include them is used
        self.assertEqual(db.count_recent_auth_failures(self.IP, NOW - LOCKOUT_WINDOW_S - 100), 0)


if __name__ == "__main__":
    unittest.main()
