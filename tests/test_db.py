"""Tests pinning the ported db.py semantics: dedupe bools, upserts, cascades,
and the entity contract on a real file database."""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db  # noqa: E402
from app.models import Invoice, Payment  # noqa: E402

def invoice(id: str, status: str = "pending", **over) -> dict:
    row = {
        "id": id, "created_at": 100, "created_height": 0, "expires_at": 200,
        "amount_atomic": 1000, "memo": id, "label": None, "metadata": "{}",
        "status": status, "paid_atomic": 0, "confirmed_at": None,
        "late_payment": 0, "overpaid": 0,
    }
    row.update(over)
    return row


def payment(txid: str, inv_id: str) -> Payment:
    return Payment(
        txid=txid, output_index=0, invoice_id=inv_id, amount_atomic=500,
        block_height=10, memo_hex=None, matched_at=150, match_method="memo",
    )


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
        p.output_index = 1
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
        self.assertEqual(db.get_payout_by_key("k1").error, "core said no")
        db.update_payout_sent("k1", "tx9", 3)
        row = db.get_payout_by_key("k1")
        self.assertEqual((row.txid, row.fee_atomic, row.error), ("tx9", 3, None))

    def test_get_payout_by_id(self):
        db.insert_payout({
            "created_at": 1, "address": "bA", "amount_atomic": 7, "memo": None,
            "txid": None, "fee_atomic": None, "idempotency_key": "k1", "error": None,
        })
        by_key = db.get_payout_by_key("k1")
        self.assertEqual(db.get_payout(by_key.id).idempotency_key, "k1")
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
        self.assertEqual(ep.url, "https://shop.example/hook")
        self.assertEqual(ep.active, 1)
        self.assertEqual(ep.secret, "s3cret")
        db.enqueue_delivery(ep.id, "invoice.confirmed", "inv-a", 0, "evt-1", "seen")
        db.delete_webhook_endpoint(ep.id)
        self.assertIsNone(db.get_webhook_endpoint(ep.id))
        self.assertEqual(db.list_deliveries(), [])
        self.assertEqual(db.deliveries_for_invoice("inv-a"), [])

    def test_pending_deliveries_joins_endpoint_credentials(self):
        ep = db.create_webhook_endpoint("https://shop.example/hook", "s3cret")
        db.enqueue_delivery(ep.id, "invoice.seen", "inv-a", 50, "evt-1", "pending")
        due = db.pending_deliveries(100)
        self.assertEqual(len(due), 1)
        self.assertEqual(due[0].endpoint_url, "https://shop.example/hook")
        self.assertEqual(due[0].endpoint_secret, "s3cret")
        self.assertEqual(due[0].delivery.event_id, "evt-1")
        self.assertEqual(due[0].delivery.attempts, 0)
        db.update_delivery(due[0].delivery.id, {"delivered_at": 101})
        self.assertEqual(db.pending_deliveries(200), [])

    def test_invoice_entity_contract(self):
        db.insert_invoice(invoice("x", metadata='{"order": 7}'))
        row = db.get_invoice("x")
        self.assertEqual(row.meta, '{"order": 7}')  # ORM attr, column name is `metadata`
        db.update_invoice("x", {"status": "cancelled", "paid_atomic": 42})
        row = db.get_invoice("x")
        self.assertEqual(row.status, "cancelled")
        self.assertEqual(row.paid_atomic, 42)

    def test_invoice_lifecycle_helpers(self):
        now = 1000
        # is_cancellable: pending/seen only
        self.assertTrue(Invoice(status="pending").is_cancellable())
        self.assertTrue(Invoice(status="seen").is_cancellable())
        for status in ("confirmed", "expired", "cancelled"):
            self.assertFalse(Invoice(status=status).is_cancellable())
        # is_expired: pending|seen AND unpaid AND past expires_at
        base = dict(status="pending", paid_atomic=0, expires_at=900)
        self.assertTrue(Invoice(**base).is_expired(now))
        self.assertTrue(Invoice(**{**base, "status": "seen"}).is_expired(now))
        self.assertFalse(Invoice(**{**base, "paid_atomic": 1}).is_expired(now))  # paid → not expired
        self.assertFalse(Invoice(**{**base, "expires_at": now}).is_expired(now))  # future/at boundary
        self.assertFalse(Invoice(**{**base, "expires_at": 2000}).is_expired(now))
        self.assertFalse(Invoice(**{**base, "status": "confirmed"}).is_expired(now))
        self.assertFalse(Invoice(**{**base, "status": "cancelled"}).is_expired(now))

    def test_match_outputs_with_entities(self):
        from app import invoices as invoices_mod

        def entity(inv_id: str, status: str, amount: int, created_at: int, expires_at: int = 9999) -> Invoice:
            return Invoice(
                id=inv_id, created_at=created_at, created_height=0, expires_at=expires_at,
                amount_atomic=amount, memo=inv_id, label=None, meta="{}",
                status=status, paid_atomic=0, confirmed_at=None, late_payment=0, overpaid=0,
            )

        settings = {"confirmations_required": "2", "amount_fallback_matching": "1"}

        # memo match: memo_hex is the hex encoding of the invoice id (ASCII)
        pending = entity("INV-ABCDEFGH", "pending", 500, 100)
        other = entity("INV-BBBBBBBB", "pending", 500, 200)
        gone = entity("INV-CCCCCCCC", "expired", 500, 50, expires_at=10)
        memo_hex = pending.id.encode("ascii").hex()
        res = invoices_mod.match_outputs(
            [{"txid": "txA", "output_index": 0, "amount_atomic": 500, "block_height": 9, "memo_hex": memo_hex}],
            chain_height=10, invoices=[pending, other, gone], payments_by_invoice={},
            existing_payment_keys=set(), settings=settings, now=150,
        )
        self.assertEqual(len(res["payments"]), 1)
        p = res["payments"][0]
        self.assertEqual((p.txid, p.output_index, p.invoice_id, p.amount_atomic), ("txA", 0, "INV-ABCDEFGH", 500))
        self.assertEqual(p.match_method, "memo")
        self.assertEqual(res["invoice_updates"]["INV-ABCDEFGH"], {"paid_atomic": 500, "status": "seen"})
        self.assertEqual([e["event"] for e in res["events"]], ["payment.seen"])
        self.assertEqual(res["events"][0]["payment"].txid, "txA")

        # amount fallback: no memo, oldest unpaid pending invoice wins
        older = entity("INV-DDDDDDDD", "pending", 700, 10)
        newer = entity("INV-EEEEEEEE", "pending", 700, 20)
        res2 = invoices_mod.match_outputs(
            [{"txid": "txB", "output_index": 0, "amount_atomic": 700, "block_height": 5, "memo_hex": None}],
            chain_height=10, invoices=[newer, older], payments_by_invoice={},
            existing_payment_keys=set(), settings=settings, now=150,
        )
        self.assertEqual(res2["payments"][0].invoice_id, "INV-DDDDDDDD")
        self.assertEqual(res2["payments"][0].match_method, "amount")


if __name__ == "__main__":
    unittest.main()
