"""Regression tests for POST /api/v1/payouts response payloads.

The send commits BEFORE FastAPI validates the handler's return against the
declared response_model — a schema miss turns a settled payout into a
client-side HTTP 500. These tests pin that both return paths (fresh send
and idempotent replay) satisfy PayoutCreatedOut, and that a replay never
resends.
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import db  # noqa: E402
from app.api import payouts  # noqa: E402
from app.api.schemas import PayoutCreatedOut, PayoutCreateRequest  # noqa: E402


class FakeCore:
    """Stands in for state.client: records sends, returns a fixed receipt."""

    def __init__(self) -> None:
        self.sent: list[tuple[list[dict], str]] = []

    async def send(self, recipients: list[dict], key: str) -> dict:
        self.sent.append((recipients, key))
        return {"txid": "535f6248deadbeef", "fee": 2500}


def fake_request(core: FakeCore) -> SimpleNamespace:
    state = SimpleNamespace(client=core)
    return SimpleNamespace(app=SimpleNamespace(state=state))


class TestCreatePayoutResponseContract(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        db.connect(str(Path(self._tmp.name) / "test.db"))
        self.core = FakeCore()
        self.request = fake_request(self.core)

    def tearDown(self):
        self._tmp.cleanup()

    def _create(self, key: str) -> dict:
        body = PayoutCreateRequest(
            address="BRTXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXXX",
            amount_atomic=1000,
            idempotency_key=key,
        )
        return asyncio.run(payouts.create_payout(body, self.request, None))

    def test_fresh_send_satisfies_response_model(self):
        res = self._create("claim-key-0001")
        self.assertFalse(res["replayed"])
        self.assertEqual(res["txid"], "535f6248deadbeef")
        self.assertEqual(res["idempotency_key"], "claim-key-0001")
        # the regression: this validate is what FastAPI does after the
        # send has already settled — a miss here was the HTTP 500
        PayoutCreatedOut.model_validate(res)

    def test_replay_returns_stored_payout_without_resending(self):
        first = self._create("claim-key-0002")
        second = self._create("claim-key-0002")
        self.assertTrue(second["replayed"])
        self.assertEqual(second["txid"], first["txid"])
        self.assertEqual(len(self.core.sent), 1)  # idempotent: no double send
        PayoutCreatedOut.model_validate(second)


if __name__ == "__main__":
    unittest.main()
