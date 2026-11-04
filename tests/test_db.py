"""Tests pinning the ported db.py semantics: dedupe bools, upserts, cascades,
and the dict-in/dict-out contract on a real file database."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db  # noqa: E402


def invoice(id: str, status: str = "pending", **over) -> dict:
    row = {
        "id": id, "created_at": 100, "created_height": 0, "expires_at": 200,
        "amount_atomic": 1000, "memo": id, "label": None, "metadata": "{}",
        "status": status, "paid_atomic": 0, "confirmed_at": None,
        "late_payment": 0, "overpaid": 0,
    }
    row.update(over)
    return row


def payment(txid: str, inv_id: str) -> dict:
    return {
        "txid": txid, "output_index": 0, "invoice_id": inv_id,
        "amount_atomic": 500, "block_height": 10, "memo_hex": None,
        "matched_at": 150, "match_method": "memo",
    }


class TestDb(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        db.connect(str(Path(self._tmp.name) / "test.db"))

    def tearDown(self):
        self._tmp.cleanup()

    def test_insert_payment_dedupes_on_txid_output_index(self):
        db.insert_invoice(invoice("a"))
        db.insert_invoice(invoice("b"))
        self.assertTrue(db.insert_payment(payment("tx1", "a")))
        self.assertFalse(db.insert_payment(payment("tx1", "a")))
        self.assertFalse(db.insert_payment(payment("tx1", "b")))  # same (txid, index) even for another invoice
        # a different output_index of the same txid is a distinct payment
        p = payment("tx1", "a")
        p["output_index"] = 1
        self.assertTrue(db.insert_payment(p))
        self.assertEqual(db.get_payment_keys(), {("tx1", 0), ("tx1", 1)})

    def test_insert_payment_missing_invoice_fails(self):
        from sqlalchemy.exc import IntegrityError

        with self.assertRaises(IntegrityError):
            db.insert_payment(payment("tx0", "nope"))

    def test_update_payout_sent_clears_error(self):
        db.insert_payout({
            "created_at": 1, "address": "bA", "amount_atomic": 7, "memo": None,
            "txid": None, "fee_atomic": None, "idempotency_key": "k1", "error": "pending",
        })
        db.update_payout_error("k1", "core said no")
        self.assertEqual(db.get_payout_by_key("k1")["error"], "core said no")
        db.update_payout_sent("k1", "tx9", 3)
        row = db.get_payout_by_key("k1")
        self.assertEqual((row["txid"], row["fee_atomic"], row["error"]), ("tx9", 3, None))

    def test_get_payout_by_id(self):
        db.insert_payout({
            "created_at": 1, "address": "bA", "amount_atomic": 7, "memo": None,
            "txid": None, "fee_atomic": None, "idempotency_key": "k1", "error": None,
        })
        by_key = db.get_payout_by_key("k1")
        self.assertEqual(db.get_payout(by_key["id"])["idempotency_key"], "k1")
        self.assertIsNone(db.get_payout(999))

    def test_invoice_status_counts(self):
        db.insert_invoice(invoice("a", "pending"))
        db.insert_invoice(invoice("b", "pending"))
        db.insert_invoice(invoice("c", "confirmed"))
        self.assertEqual(db.invoice_status_counts(), {"pending": 2, "confirmed": 1})

    def test_set_setting_upserts(self):
        db.set_setting("expiry_minutes", "90")
        self.assertEqual(db.get_setting("expiry_minutes"), "90")
        db.set_setting("expiry_minutes", "120")
        self.assertEqual(db.get_setting("expiry_minutes"), "120")
        self.assertEqual(db.get_setting("missing", "fallback"), "fallback")

    def test_delete_webhook_endpoint_removes_deliveries(self):
        ep = db.create_webhook_endpoint("https://shop.example/hook", "s3cret")
        self.assertEqual(ep["url"], "https://shop.example/hook")
        self.assertEqual(ep["active"], 1)
        self.assertEqual(ep["secret"], "s3cret")
        db.enqueue_delivery(ep["id"], "invoice.confirmed", "inv-a", 0, "evt-1", "seen")
        db.delete_webhook_endpoint(ep["id"])
        self.assertIsNone(db.get_webhook_endpoint(ep["id"]))
        self.assertEqual(db.list_deliveries(), [])
        self.assertEqual(db.deliveries_for_invoice("inv-a"), [])

    def test_pending_deliveries_joins_endpoint_credentials(self):
        ep = db.create_webhook_endpoint("https://shop.example/hook", "s3cret")
        db.enqueue_delivery(ep["id"], "invoice.seen", "inv-a", 50, "evt-1", "pending")
        due = db.pending_deliveries(100)
        self.assertEqual(len(due), 1)
        self.assertEqual(due[0]["endpoint_url"], "https://shop.example/hook")
        self.assertEqual(due[0]["endpoint_secret"], "s3cret")
        self.assertEqual(due[0]["event_id"], "evt-1")
        self.assertEqual(due[0]["attempts"], 0)
        db.update_delivery(due[0]["id"], {"delivered_at": 101})
        self.assertEqual(db.pending_deliveries(200), [])

    def test_invoice_dict_contract(self):
        db.insert_invoice(invoice("x", metadata='{"order": 7}'))
        row = db.get_invoice("x")
        self.assertIn("metadata", row)  # column name, not the ORM attribute
        self.assertEqual(row["metadata"], '{"order": 7}')
        db.update_invoice("x", {"status": "cancelled", "paid_atomic": 42})
        row = db.get_invoice("x")
        self.assertEqual(row["status"], "cancelled")
        self.assertEqual(row["paid_atomic"], 42)


if __name__ == "__main__":
    unittest.main()
