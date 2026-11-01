"""Unit tests for the pure matcher and invoice amount helpers (stdlib unittest)."""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.invoices import (  # noqa: E402
    AmountError,
    find_memo_invoice_id,
    format_bnt,
    invoice_uri,
    match_outputs,
    parse_amount_bnt,
)

SETTINGS = {"confirmations_required": "2", "amount_fallback_matching": "1"}
NOW = 1_700_000_000


def inv(inv_id: str, amount: int, status="pending", paid=0, created=NOW, expires=NOW + 3600, **kw):
    return {
        "id": inv_id,
        "created_at": created,
        "expires_at": expires,
        "amount_atomic": amount,
        "memo": inv_id,
        "label": None,
        "metadata": {},
        "status": status,
        "paid_atomic": paid,
        "confirmed_at": None,
        "late_payment": 0,
        "overpaid": 0,
        **kw,
    }


def out(txid: str, amount: int, memo_text: str | None, height: int = 100, index=0):
    memo_hex = memo_text.encode().hex() if memo_text is not None else None
    return {
        "txid": txid,
        "output_index": index,
        "amount": amount,
        "block_height": height,
        "memo_hex": memo_hex,
    }


class TestAmounts(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(parse_amount_bnt("0.1"), 10_000_000)
        self.assertEqual(parse_amount_bnt(1), 100_000_000)
        self.assertEqual(parse_amount_bnt("12.00000001"), 1_200_000_001)
        self.assertEqual(parse_amount_bnt("0.00000001"), 1)

    def test_parse_rejects(self):
        for bad in ("0", "-1", "abc", "0.000000001", None, ""):
            with self.assertRaises(AmountError, msg=bad):
                parse_amount_bnt(bad)

    def test_format(self):
        self.assertEqual(format_bnt(10_000_000), "0.1")
        self.assertEqual(format_bnt(100_000_000), "1")
        self.assertEqual(format_bnt(1_200_000_001), "12.00000001")
        self.assertEqual(format_bnt(1250000000), "12.5")

    def test_uri(self):
        self.assertEqual(
            invoice_uri("ULPADDR", 10_000_000, "INV-ABC234DE"),
            "blocknet://ULPADDR?amount=0.1&memo=INV-ABC234DE",
        )


class TestMemoExtraction(unittest.TestCase):
    def test_exact(self):
        hexed = "pay invoice INV-ABCDEFGH now".encode().hex()
        self.assertEqual(find_memo_invoice_id(hexed), "INV-ABCDEFGH")

    def test_none(self):
        self.assertIsNone(find_memo_invoice_id("hello".encode().hex()))
        self.assertIsNone(find_memo_invoice_id(None))
        self.assertIsNone(find_memo_invoice_id(""))
        self.assertIsNone(find_memo_invoice_id("zz"))  # not hex

    def test_invalid_chars_not_matched(self):
        # 0/1/I/O are excluded from the alphabet; must not match an id containing them
        self.assertIsNone(find_memo_invoice_id("INV-ABCD0FGH".encode().hex()))


class TestMatching(unittest.TestCase):
    def run_match(self, outputs, invoices, payments=None, keys=None, settings=SETTINGS, chain=102):
        return match_outputs(
            outputs, chain, invoices, payments or {}, keys or set(), settings, now=NOW
        )

    def test_memo_match_mempool_seen(self):
        invoices = [inv("INV-ABCDEFGH", 10_000_000)]
        res = self.run_match([out("tx1", 10_000_000, "INV-ABCDEFGH", height=0)], invoices)
        self.assertEqual(len(res["payments"]), 1)
        p = res["payments"][0]
        self.assertEqual((p["invoice_id"], p["match_method"]), ("INV-ABCDEFGH", "memo"))
        self.assertEqual(res["invoice_updates"]["INV-ABCDEFGH"]["status"], "seen")
        self.assertNotIn("confirmed_at", res["invoice_updates"]["INV-ABCDEFGH"])
        self.assertEqual([e["event"] for e in res["events"]], ["payment.seen"])

    def test_memo_match_confirms_at_threshold(self):
        invoices = [inv("INV-ABCDEFGH", 10_000_000)]
        # chain 102, height 100 -> 2 confirmations == required -> confirmed
        res = self.run_match([out("tx1", 10_000_000, "INV-ABCDEFGH", height=100)], invoices)
        upd = res["invoice_updates"]["INV-ABCDEFGH"]
        self.assertEqual(upd["status"], "confirmed")
        self.assertEqual(upd["confirmed_at"], NOW)
        self.assertEqual(
            [e["event"] for e in res["events"]], ["payment.seen", "invoice.confirmed"]
        )

    def test_underpayment_partial_then_topup_confirms(self):
        invoices = [inv("INV-ABCDEFGH", 20_000_000)]
        res = self.run_match(
            [out("tx1", 10_000_000, "INV-ABCDEFGH", height=100),
             out("tx2", 10_000_000, "INV-ABCDEFGH", height=101)],
            invoices,
            chain=103,  # confirmations 3 and 2; min = 2 == required
        )
        upd = res["invoice_updates"]["INV-ABCDEFGH"]
        self.assertEqual(upd["status"], "confirmed")
        self.assertEqual(upd["paid_atomic"], 20_000_000)
        self.assertNotIn("overpaid", upd)  # only written when it flips to 1

    def test_min_confirmations_across_payments(self):
        invoices = [inv("INV-ABCDEFGH", 20_000_000)]
        res = self.run_match(
            [out("tx1", 10_000_000, "INV-ABCDEFGH", height=100),
             out("tx2", 10_000_000, "INV-ABCDEFGH", height=102)],  # mempool
            invoices,
        )
        upd = res["invoice_updates"]["INV-ABCDEFGH"]
        self.assertEqual(upd["status"], "seen")  # one payment still mempool
        self.assertEqual(upd["paid_atomic"], 20_000_000)

    def test_overpaid_flag(self):
        invoices = [inv("INV-ABCDEFGH", 5_000_000)]
        res = self.run_match([out("tx1", 6_000_000, "INV-ABCDEFGH", height=100)], invoices)
        upd = res["invoice_updates"]["INV-ABCDEFGH"]
        self.assertEqual(upd["status"], "confirmed")
        self.assertEqual(upd["overpaid"], 1)
        self.assertEqual(upd["paid_atomic"], 6_000_000)

    def test_double_credit_prevention(self):
        invoices = [inv("INV-ABCDEFGH", 10_000_000)]
        outputs = [out("tx1", 10_000_000, "INV-ABCDEFGH", height=100)]
        res1 = self.run_match(outputs, invoices)
        keys = {(p["txid"], p["output_index"]) for p in res1["payments"]}
        pays = {"INV-ABCDEFGH": res1["payments"]}
        res2 = match_outputs(outputs, 102, invoices, pays, keys, SETTINGS, now=NOW + 5)
        self.assertEqual(res2["payments"], [])
        self.assertEqual(res2["events"], [])
        self.assertEqual(res2["invoice_updates"], {})

    def test_amount_fallback_oldest_first(self):
        older = inv("INV-AAAAAAAA", 7_000_000, created=NOW - 500)
        newer = inv("INV-BBBBBBBB", 7_000_000, created=NOW)
        res = self.run_match([out("tx1", 7_000_000, None, height=100)], [newer, older])
        self.assertEqual(res["payments"][0]["invoice_id"], "INV-AAAAAAAA")
        self.assertEqual(res["payments"][0]["match_method"], "amount")

    def test_amount_fallback_skips_partially_paid(self):
        invoices = [inv("INV-AAAAAAAA", 7_000_000, paid=3_000_000)]
        res = self.run_match([out("tx1", 7_000_000, None, height=100)], invoices)
        self.assertEqual(res["payments"], [])

    def test_amount_fallback_disabled(self):
        settings = dict(SETTINGS, amount_fallback_matching="0")
        invoices = [inv("INV-AAAAAAAA", 7_000_000)]
        res = self.run_match([out("tx1", 7_000_000, None, height=100)], invoices, settings=settings)
        self.assertEqual(res["payments"], [])

    def test_memo_wins_over_amount(self):
        target = inv("INV-AAAAAAAA", 7_000_000, created=NOW - 999)
        other = inv("INV-BBBBBBBB", 7_000_000, created=NOW)
        res = self.run_match([out("tx1", 7_000_000, "INV-BBBBBBBB", height=100)], [target, other])
        self.assertEqual(res["payments"][0]["invoice_id"], "INV-BBBBBBBB")
        self.assertEqual(res["payments"][0]["match_method"], "memo")

    def test_memo_matches_expired_invoice_late_payment(self):
        invoices = [inv("INV-AAAAAAAA", 10_000_000, status="expired", expires=NOW - 10)]
        res = self.run_match([out("tx1", 10_000_000, "INV-AAAAAAAA", height=100)], invoices)
        upd = res["invoice_updates"]["INV-AAAAAAAA"]
        self.assertEqual(upd["late_payment"], 1)
        self.assertEqual(upd["status"], "confirmed")

    def test_amount_fallback_never_matches_expired(self):
        invoices = [inv("INV-AAAAAAAA", 7_000_000, status="expired", expires=NOW - 10)]
        res = self.run_match([out("tx1", 7_000_000, None, height=100)], invoices)
        self.assertEqual(res["payments"], [])

    def test_confirmed_invoice_not_rematched_by_amount(self):
        invoices = [inv("INV-AAAAAAAA", 7_000_000, status="confirmed", paid=7_000_000)]
        res = self.run_match([out("tx9", 7_000_000, None, height=100)], invoices)
        self.assertEqual(res["payments"], [])

    def test_cancelled_never_matched(self):
        invoices = [inv("INV-AAAAAAAA", 7_000_000, status="cancelled")]
        res = self.run_match([out("tx1", 7_000_000, "INV-AAAAAAAA", height=100)], invoices)
        self.assertEqual(res["payments"], [])

    def test_unmatched_output_ignored(self):
        invoices = [inv("INV-AAAAAAAA", 7_000_000)]
        res = self.run_match([out("tx1", 999, "note but no id", height=100)], invoices)
        self.assertEqual(res["payments"], [])

    def test_metadata_json_roundtrip(self):
        # invoices stored in DB carry metadata as TEXT; matcher must not choke
        i = inv("INV-AAAAAAAA", 7_000_000)
        i["metadata"] = json.dumps({"order": 1})
        res = self.run_match([out("tx1", 7_000_000, "INV-AAAAAAAA", height=100)], [i])
        self.assertEqual(res["payments"][0]["invoice_id"], "INV-AAAAAAAA")


if __name__ == "__main__":
    unittest.main()


class TestRecomputeConfirmations(unittest.TestCase):


    def test_seen_confirms_when_blocks_land(self):
        from app.invoices import recompute_confirmations
        invoices = [inv("INV-AAAAAAAA", 10_000_000, status="seen", paid=10_000_000)]
        pays = {"INV-AAAAAAAA": [{"txid": "tx1", "output_index": 0, "amount_atomic": 10_000_000, "block_height": 100}]}
        # match happened at height 100 with required 2 -> stayed seen; now chain 103
        res = recompute_confirmations(invoices, pays, 103, SETTINGS, now=NOW)
        self.assertEqual(res["invoice_updates"]["INV-AAAAAAAA"]["status"], "confirmed")
        self.assertEqual([e["event"] for e in res["events"]], ["invoice.confirmed"])

    def test_below_threshold_unchanged(self):
        from app.invoices import recompute_confirmations
        invoices = [inv("INV-AAAAAAAA", 10_000_000, status="seen", paid=10_000_000)]
        pays = {"INV-AAAAAAAA": [{"txid": "tx1", "output_index": 0, "amount_atomic": 10_000_000, "block_height": 102}]}
        res = recompute_confirmations(invoices, pays, 103, SETTINGS, now=NOW)  # conf 1 < 2
        self.assertEqual(res["invoice_updates"], {})
        self.assertEqual(res["events"], [])

    def test_mempool_payment_not_confirmed(self):
        from app.invoices import recompute_confirmations
        invoices = [inv("INV-AAAAAAAA", 10_000_000, status="seen", paid=10_000_000)]
        pays = {"INV-AAAAAAAA": [{"txid": "tx1", "output_index": 0, "amount_atomic": 10_000_000, "block_height": 0}]}
        res = recompute_confirmations(invoices, pays, 999, SETTINGS, now=NOW)
        self.assertEqual(res["invoice_updates"], {})

    def test_underpaid_seen_not_confirmed(self):
        from app.invoices import recompute_confirmations
        invoices = [inv("INV-AAAAAAAA", 20_000_000, status="seen", paid=10_000_000)]
        pays = {"INV-AAAAAAAA": [{"txid": "tx1", "output_index": 0, "amount_atomic": 10_000_000, "block_height": 100}]}
        res = recompute_confirmations(invoices, pays, 200, SETTINGS, now=NOW)
        self.assertEqual(res["invoice_updates"], {})

    def test_expired_late_payment_confirms_with_flag(self):
        from app.invoices import recompute_confirmations
        invoices = [inv("INV-AAAAAAAA", 10_000_000, status="expired", paid=10_000_000)]
        pays = {"INV-AAAAAAAA": [{"txid": "tx1", "output_index": 0, "amount_atomic": 10_000_000, "block_height": 100}]}
        res = recompute_confirmations(invoices, pays, 103, SETTINGS, now=NOW)
        self.assertEqual(res["invoice_updates"]["INV-AAAAAAAA"]["late_payment"], 1)
        self.assertEqual(res["invoice_updates"]["INV-AAAAAAAA"]["status"], "confirmed")

    def test_pending_without_payments_ignored(self):
        from app.invoices import recompute_confirmations
        invoices = [inv("INV-AAAAAAAA", 10_000_000, status="pending")]
        res = recompute_confirmations(invoices, {}, 103, SETTINGS, now=NOW)
        self.assertEqual(res["invoice_updates"], {})


class TestInvoiceJson(unittest.TestCase):
    def test_flags_and_urls(self):
        from app.invoices import invoice_to_json
        base = {"confirmations_required": "10", "display_handle": "$h"}
        i = inv("INV-AAAAAAAA", 10_000_000, status="seen", paid=4_000_000)
        out = invoice_to_json(i, [], settings=base, public_base_url="http://x:1",
                              address="ULPADDR", chain_height=100)
        self.assertTrue(out["underpaid"])
        self.assertFalse(out["overpaid"])
        self.assertEqual(out["qr_url"], "http://x:1/pay/INV-AAAAAAAA/qr.svg")
        self.assertEqual(out["pay_url"], "http://x:1/pay/INV-AAAAAAAA")

    def test_flags_fully_paid_and_unpaid(self):
        from app.invoices import invoice_to_json
        base = {"confirmations_required": "10", "display_handle": ""}
        paid = invoice_to_json(inv("INV-AAAAAAAA", 10_000_000, status="confirmed", paid=10_000_000),
                               [], settings=base, public_base_url="http://x:1", address="A", chain_height=5)
        self.assertFalse(paid["underpaid"])
        self.assertFalse(paid["underpaid"])
        fresh = invoice_to_json(inv("INV-BBBBBBBB", 10_000_000, status="pending"),
                                [], settings=base, public_base_url="http://x:1", address="A", chain_height=5)
        self.assertFalse(fresh["underpaid"])
        over = invoice_to_json(inv("INV-CCCCCCCC", 10_000_000, status="confirmed", paid=11_000_000, overpaid=1),
                               [], settings=base, public_base_url="http://x:1", address="A", chain_height=5)
        self.assertFalse(over["underpaid"])
        self.assertTrue(over["overpaid"])

    def test_events_carry_previous_status(self):
        invoices_ = [inv("INV-AAAAAAAA", 10_000_000)]
        res = match_outputs([out("tx1", 10_000_000, "INV-AAAAAAAA", height=100)], 102,
                            invoices_, {}, set(), SETTINGS, now=NOW)
        self.assertEqual(res["events"][0]["previous_status"], "pending")
        self.assertEqual(res["events"][1]["previous_status"], "pending")

