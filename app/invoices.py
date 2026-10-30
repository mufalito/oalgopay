"""Invoice model: id/memo minting, URI construction, and the pure payment matcher.

The matcher is a pure function (no I/O): it takes daemon wallet outputs plus
current invoice/payment state and returns payments to insert, invoice field
updates, and webhook events. Idempotency comes from the payments PK
(txid, output_index).
"""


import secrets
import time
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import quote

ID_PREFIX = "INV-"
ID_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
ID_LEN = 8
ATOMS_PER_BNT = 100_000_000


ACTIVE_STATUSES = ("pending", "seen")


class AmountError(ValueError):
    pass


def mint_id() -> str:
    return ID_PREFIX + "".join(secrets.choice(ID_ALPHABET) for _ in range(ID_LEN))


def is_valid_id(inv_id: str) -> bool:
    return (
        len(inv_id) == len(ID_PREFIX) + ID_LEN
        and inv_id.startswith(ID_PREFIX)
        and all(c in ID_ALPHABET for c in inv_id[len(ID_PREFIX):])
    )


def parse_amount_bnt(value: Any) -> int:
    """BNT amount (str/number/Decimal, <= 8 decimal places, > 0) -> atomic units."""
    try:
        dec = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise AmountError(f"amount_bnt not a number: {value!r}") from exc
    if dec <= 0:
        raise AmountError("amount_bnt must be > 0")
    atoms = dec * ATOMS_PER_BNT
    if atoms != atoms.to_integral_value():
        raise AmountError("amount_bnt has more than 8 decimal places")
    return int(atoms)


def format_bnt(amount_atomic: int) -> str:
    """Atomic units -> plain decimal string, no trailing zeros, no exponent."""
    dec = (Decimal(amount_atomic) / ATOMS_PER_BNT).quantize(Decimal("0.00000001"))
    text = format(dec.normalize(), "f")
    return text


def format_bnt_padded(amount_atomic: int) -> str:
    """Atomic units -> fixed 8-dp string for display."""
    return f"{Decimal(amount_atomic) / ATOMS_PER_BNT:.8f}"


def build_invoice(
    *,
    now: int,
    expiry_minutes: int,
    amount_atomic: int,
    label: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "id": mint_id(),
        "created_at": now,
        "expires_at": now + expiry_minutes * 60,
        "amount_atomic": amount_atomic,
        "memo": "",  # filled by caller after mint (memo == id)
        "label": label,
        "metadata": metadata or {},
        "status": "pending",
        "paid_atomic": 0,
        "confirmed_at": None,
        "late_payment": 0,
        "overpaid": 0,
    }


# --- derived URLs ----------------------------------------------------------

def invoice_uri(address: str, amount_atomic: int, memo: str) -> str:
    return f"blocknet://{address}?amount={format_bnt(amount_atomic)}&memo={quote(memo, safe='')}"


def bntpay_url(display_handle: str, address: str, amount_atomic: int, memo: str) -> str:
    target = display_handle if display_handle else address
    return f"https://bntpay.com/{quote(target, safe='$')}?amount={format_bnt(amount_atomic)}&memo={quote(memo, safe='')}"


def invoice_to_json(
    inv: dict[str, Any],
    payments: list[dict[str, Any]],
    *,
    settings: dict[str, str],
    public_base_url: str,
    address: str,
    chain_height: int | None = None,
) -> dict[str, Any]:
    confirmations_required = int(settings.get("confirmations_required", "10"))
    handle = settings.get("display_handle", "")
    out = {
        "id": inv["id"],
        "status": inv["status"],
        "amount_atomic": inv["amount_atomic"],
        "amount_bnt": format_bnt(inv["amount_atomic"]),
        "memo": inv["memo"],
        "label": inv["label"],
        "metadata": inv["metadata"] if isinstance(inv["metadata"], dict) else _maybe_json(inv["metadata"]),
        "created_at": inv["created_at"],
        "expires_at": inv["expires_at"],
        "paid_atomic": inv["paid_atomic"],
        "paid_bnt": format_bnt(inv["paid_atomic"]),
        "confirmations_required": confirmations_required,
        "late_payment": bool(inv["late_payment"]),
        "overpaid": bool(inv["overpaid"]),
        "underpaid": 0 < inv["paid_atomic"] < inv["amount_atomic"],
        "confirmed_at": inv["confirmed_at"],
        "uri": invoice_uri(address, inv["amount_atomic"], inv["memo"]),
        "bntpay_url": bntpay_url(handle, address, inv["amount_atomic"], inv["memo"]),
        "pay_url": f"{public_base_url}/pay/{inv['id']}",
        "qr_url": f"{public_base_url}/pay/{inv['id']}/qr.svg",
    }
    if payments is not None:
        out["payments"] = [
            {
                "txid": p["txid"],
                "output_index": p["output_index"],
                "amount_atomic": p["amount_atomic"],
                "amount_bnt": format_bnt(p["amount_atomic"]),
                "block_height": p["block_height"],
                "confirmations": (
                    0
                    if p["block_height"] == 0 or not chain_height
                    else max(0, chain_height - p["block_height"])
                ),
                "matched_at": p["matched_at"],
                "match_method": p["match_method"],
            }
            for p in payments
        ]
    return out


def _maybe_json(text: Any) -> Any:
    import json

    if isinstance(text, str) and text:
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return {}
    return {}


# --- matcher ---------------------------------------------------------------

def find_memo_invoice_id(memo_hex: str | None) -> str | None:
    """Decode memo_hex (hex of UTF-8) and extract an invoice id, if present."""
    if not memo_hex:
        return None
    try:
        text = bytes.fromhex(memo_hex).decode("utf-8", errors="ignore")
    except ValueError:
        return None
    import re

    m = re.search(rf"INV-[{ID_ALPHABET}]{{{ID_LEN}}}", text)
    return m.group(0) if m else None


def _confirmations(block_height: int, chain_height: int) -> int:
    return 0 if block_height == 0 else max(0, chain_height - block_height)


def match_outputs(
    outputs: list[dict[str, Any]],
    chain_height: int,
    invoices: list[dict[str, Any]],
    payments_by_invoice: dict[str, list[dict[str, Any]]],
    existing_payment_keys: set[tuple[str, int]],
    settings: dict[str, str],
    now: int | None = None,
) -> dict[str, Any]:
    """Pure matcher. Returns {"payments": [...], "invoice_updates": {id: {...}}, "events": [...]}.

    Matching order per output (skipping (txid, output_index) already credited):
      1. memo: decoded memo contains an invoice id whose invoice is
         pending|seen|expired  -> method "memo"
      2. amount fallback (if amount_fallback_matching=1): oldest invoice with
         status pending|seen, amount_atomic == output amount, paid_atomic == 0
         -> method "amount"
    After a new payment, the invoice is recomputed:
      confirmations = min over its payments (0 for mempool)
      paid_atomic >= amount_atomic and confirmations >= required -> confirmed
      else status stays/becomes "seen"
    """
    now = int(time.time()) if now is None else now
    confirmations_required = int(settings.get("confirmations_required", "10"))
    fallback_enabled = settings.get("amount_fallback_matching", "1") == "1"

    by_id = {inv["id"]: dict(inv) for inv in invoices}
    pays: dict[str, list[dict[str, Any]]] = {i: [dict(p) for p in ps] for i, ps in payments_by_invoice.items()}
    new_payments: list[dict[str, Any]] = []
    invoice_updates: dict[str, dict[str, Any]] = {}
    events: list[dict[str, Any]] = []

    def record_update(inv_id: str, fields: dict[str, Any]) -> None:
        merged = invoice_updates.get(inv_id, {})
        merged.update(fields)
        invoice_updates[inv_id] = merged

    for out in outputs:
        key = (str(out.get("txid", "")), int(out.get("output_index", out.get("index", 0))))
        if key in existing_payment_keys:
            continue
        amount = int(out.get("amount_atomic", out.get("amount", 0)))
        block_height = int(out.get("block_height", 0))
        memo_hex = out.get("memo_hex")

        target_id = None
        method = None

        memo_inv = find_memo_invoice_id(memo_hex)
        if memo_inv and memo_inv in by_id and by_id[memo_inv]["status"] in ("pending", "seen", "expired"):
            target_id, method = memo_inv, "memo"
        elif fallback_enabled and amount > 0:
            candidates = [
                inv
                for inv in by_id.values()
                if inv["status"] in ACTIVE_STATUSES
                and inv["amount_atomic"] == amount
                and inv["paid_atomic"] == 0
            ]
            if candidates:
                oldest = min(candidates, key=lambda inv: inv["created_at"])
                target_id, method = oldest["id"], "amount"

        if not target_id:
            continue

        payment = {
            "txid": key[0],
            "output_index": key[1],
            "invoice_id": target_id,
            "amount_atomic": amount,
            "block_height": block_height,
            "memo_hex": memo_hex,
            "matched_at": now,
            "match_method": method,
        }
        new_payments.append(payment)
        pays.setdefault(target_id, []).append(payment)
        existing_payment_keys = existing_payment_keys | {key}

        inv = by_id[target_id]
        inv_status_before = inv["status"]
        was_expired = inv_status_before == "expired"
        was_confirmed = inv_status_before == "confirmed"
        inv_payments = pays[target_id]
        paid_total = sum(p["amount_atomic"] for p in inv_payments)
        confs = min(_confirmations(p["block_height"], chain_height) for p in inv_payments)
        confirmed = paid_total >= inv["amount_atomic"] and confs >= confirmations_required

        inv["paid_atomic"] = paid_total
        fields: dict[str, Any] = {"paid_atomic": paid_total}
        if was_expired:
            fields["late_payment"] = 1
        if not was_confirmed and confirmed:
            inv["status"] = "confirmed"
            inv["confirmed_at"] = now
            fields.update({"status": "confirmed", "confirmed_at": now})
            if paid_total > inv["amount_atomic"]:
                fields["overpaid"] = 1
        else:
            if inv["status"] == "pending":
                inv["status"] = "seen"
                fields["status"] = "seen"
            if was_confirmed and paid_total > inv["amount_atomic"]:
                fields["overpaid"] = 1
        record_update(target_id, fields)

        if not was_confirmed:
            events.append({
                "event": "payment.seen", "invoice_id": target_id, "payment": payment,
                "previous_status": inv_status_before,
            })
        if confirmed and not was_confirmed:
            events.append({
                "event": "invoice.confirmed", "invoice_id": target_id, "payment": payment,
                "previous_status": inv_status_before,
            })

    return {"payments": new_payments, "invoice_updates": invoice_updates, "events": events}


def recompute_confirmations(
    invoices: list[dict[str, Any]],
    payments_by_invoice: dict[str, list[dict[str, Any]]],
    chain_height: int,
    settings: dict[str, str],
    now: int,
) -> dict[str, Any]:
    """Pure confirmation sweep: move seen/expired invoices with sufficient
    confirmations to confirmed. Complements match_outputs, which only updates
    an invoice when a NEW output arrives — this closes the gap when payments
    were matched before reaching the confirmation threshold.

    Returns {"invoice_updates": {id: {...}}, "events": [...]}.
    """
    required = int(settings.get("confirmations_required", "10"))
    invoice_updates: dict[str, dict[str, Any]] = {}
    events: list[dict[str, Any]] = []
    for inv in invoices:
        if inv["status"] not in ("seen", "expired"):
            continue
        payments = payments_by_invoice.get(inv["id"], [])
        if not payments:
            continue
        paid_total = sum(p["amount_atomic"] for p in payments)
        if paid_total < inv["amount_atomic"]:
            continue
        confs = min(_confirmations(p["block_height"], chain_height) for p in payments)
        if confs < required:
            continue
        fields: dict[str, Any] = {"status": "confirmed", "confirmed_at": now}
        if inv["status"] == "expired":
            fields["late_payment"] = 1
        if paid_total > inv["amount_atomic"] and not inv["overpaid"]:
            fields["overpaid"] = 1
        invoice_updates[inv["id"]] = fields
        events.append({
            "event": "invoice.confirmed",
            "invoice_id": inv["id"],
            "payment": payments[-1],
            "previous_status": inv["status"],
        })
    return {"invoice_updates": invoice_updates, "events": events}
