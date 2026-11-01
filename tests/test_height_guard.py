"""Tests for the amount-fallback height guard (outputs must not predate invoices)."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.invoices import match_outputs  # noqa: E402

SETTINGS = {"confirmations_required": "2", "amount_fallback_matching": "1"}
NOW = 1_700_000_000


def inv(inv_id, amount, status="pending", paid=0, created=NOW, expires=NOW + 3600, **kw):
    return {
        "id": inv_id, "created_at": created, "expires_at": expires,
        "amount_atomic": amount, "memo": inv_id, "label": None, "metadata": {},
        "status": status, "paid_atomic": paid, "confirmed_at": None,
        "late_payment": 0, "overpaid": 0, **kw,
    }


def out(txid, amount, memo_text, height=100, index=0):
    return {
        "txid": txid, "output_index": index, "amount": amount,
        "block_height": height,
        "memo_hex": memo_text.encode().hex() if memo_text else None,
    }


class TestAmountFallbackHeightGuard(unittest.TestCase):
    def _match(self, output_height, created_height):
        i = inv("INV-AAAAAAAA", 10_000_000)
        i["created_height"] = created_height
        return match_outputs(
            [out("tx1", 10_000_000, None, height=output_height)],
            202, [i], {}, set(), SETTINGS, now=NOW,
        )

    def test_old_output_never_matches_new_invoice(self):
        """An output from block 100 must not match an invoice created at block 200."""
        res = self._match(output_height=100, created_height=200)
        self.assertEqual(res["payments"], [])

    def test_mempool_always_eligible(self):
        """Mempool outputs (height 0) always pass the height guard."""
        res = self._match(output_height=0, created_height=200)
        self.assertEqual(len(res["payments"]), 1)
        self.assertEqual(res["payments"][0]["match_method"], "amount")

    def test_output_at_creation_height_matches(self):
        """An output at the exact creation height matches (edge case)."""
        res = self._match(output_height=200, created_height=200)
        self.assertEqual(len(res["payments"]), 1)

    def test_output_after_creation_matches(self):
        """An output at block 201 (after invoice created at 200) matches."""
        res = self._match(output_height=201, created_height=200)
        self.assertEqual(len(res["payments"]), 1)


if __name__ == "__main__":
    unittest.main()
