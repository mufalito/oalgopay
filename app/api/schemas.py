"""Pydantic schemas for the OpenAPI docs (/docs, /redoc) + request validation."""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, model_validator

from ..invoices import AmountError, parse_amount_bnt


class InvoiceCreateRequest(BaseModel):
    """Exactly one of `amount_bnt` / `amount_atomic` is required."""

    amount_bnt: str | float | None = Field(
        default=None, description="Decimal BNT amount, ≤ 8 decimal places, > 0 (e.g. \"12.5\")"
    )
    amount_atomic: int | None = Field(
        default=None, gt=0, description="Atomic units (1 BNT = 100000000). Mutually exclusive with amount_bnt."
    )
    label: str | None = Field(default=None, max_length=200, description="Free-form display label")
    expires_in_min: int | None = Field(
        default=None, ge=1, le=10080,
        description="Invoice lifetime in minutes (1–10080). Default: expiry_minutes setting (60).",
    )
    metadata: dict[str, Any] | None = Field(default=None, description="Arbitrary JSON echoed back verbatim")

    amount_atomic_resolved: int = Field(default=0, exclude=True)

    @model_validator(mode="after")
    def _resolve_amount(self) -> "InvoiceCreateRequest":
        if (self.amount_bnt is None) == (self.amount_atomic is None):
            raise ValueError("provide exactly one of amount_bnt, amount_atomic")
        if self.amount_bnt is not None:
            try:
                self.amount_atomic_resolved = parse_amount_bnt(self.amount_bnt)
            except AmountError as exc:
                raise ValueError(str(exc)) from exc
        else:
            self.amount_atomic_resolved = int(self.amount_atomic)  # type: ignore[arg-type]
        return self


class PaymentOut(BaseModel):
    txid: str
    output_index: int
    amount_atomic: int
    amount_bnt: str
    block_height: int
    confirmations: int
    matched_at: int
    match_method: str = Field(description='"memo" | "amount"')


class InvoiceOut(BaseModel):
    id: str = Field(description="Invoice id; also the payment memo")
    status: str = Field(description="pending | seen | confirmed | expired | cancelled")
    amount_atomic: int
    amount_bnt: str
    memo: str
    label: str | None
    metadata: dict[str, Any]
    created_at: int
    expires_at: int
    paid_atomic: int
    paid_bnt: str
    confirmations_required: int
    late_payment: bool
    overpaid: bool
    underpaid: bool = Field(description="true while 0 < paid < amount (partial payment)")
    confirmed_at: int | None
    uri: str = Field(description="blocknet:// payment URI (also encoded by the QR)")
    bntpay_url: str
    pay_url: str = Field(description="Hosted pay page — the link to give customers")
    qr_url: str
    payments: list[PaymentOut] = Field(default_factory=list)


class InvoiceListOut(BaseModel):
    invoices: list[InvoiceOut]
    limit: int
    offset: int


class PayStatusPayment(BaseModel):
    txid: str
    amount_atomic: int
    amount_bnt: str
    confirmations: int


class PayStatusOut(BaseModel):
    id: str
    status: str
    paid_atomic: int
    paid_bnt: str
    confirmations: int
    confirmations_required: int
    expires_at: int
    late_payment: bool
    overpaid: bool
    underpaid: bool
    payments: list[PayStatusPayment]


class SettingsOut(BaseModel):
    confirmations_required: str
    expiry_minutes: str
    amount_fallback_matching: str = Field(description='"1" enabled, "0" memo-only')
    display_handle: str
    last_known_address: str


class WebhookCreateRequest(BaseModel):
    url: str = Field(description="http(s) URL of your receiver")


class WebhookOut(BaseModel):
    id: int
    url: str
    active: bool
    secret: str = Field(description="Signing secret — shown once, store it now")


class SendRequest(BaseModel):
    """Manual payout/refund. Exactly one of amount_bnt / amount_atomic."""

    address: str = Field(min_length=8, description="recipient address (ULP…)")
    amount_bnt: str | float | None = Field(default=None, description="decimal BNT, ≤ 8 dp")
    amount_atomic: int | None = Field(default=None, gt=0, description="atomic units (1 BNT = 1e8)")
    memo: str | None = Field(default=None, max_length=200)

    amount_atomic_resolved: int = Field(default=0, exclude=True)

    @model_validator(mode="after")
    def _resolve_amount(self) -> "SendRequest":
        if (self.amount_bnt is None) == (self.amount_atomic is None):
            raise ValueError("provide exactly one of amount_bnt, amount_atomic")
        if self.amount_bnt is not None:
            try:
                self.amount_atomic_resolved = parse_amount_bnt(self.amount_bnt)
            except AmountError as exc:
                raise ValueError(str(exc)) from exc
        else:
            self.amount_atomic_resolved = int(self.amount_atomic)  # type: ignore[arg-type]
        return self


class SendResult(BaseModel):
    txid: str
    address: str
    amount_atomic: int
    fee_atomic: int | None = None
    memo: str | None = None


class PayoutOut(BaseModel):
    id: int
    created_at: int
    address: str
    amount_atomic: int
    memo: str | None
    txid: str | None
    fee_atomic: int | None
    error: str | None
