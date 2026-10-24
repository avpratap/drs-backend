"""Shared response schemas for routes that return small status/result dicts.

These exist so the OpenAPI spec is fully typed (drives Dart/Orval client codegen).
Each matches the exact keys the corresponding route already returns — no behaviour
change, only an explicit contract.
"""
from pydantic import BaseModel, ConfigDict, model_validator


class DetailOut(BaseModel):
    detail: str


class IdStatusOut(BaseModel):
    id: int
    status: str


class EraseResultOut(BaseModel):
    detail: str
    user_id: int
    retained: str


class OutboxFlushOut(BaseModel):
    processed: int
    sent: int
    failed: int


class DataExportOut(BaseModel):
    """DPDP data export. Values are intentionally loose (dict/list) — this is a
    portability dump, not a stable typed resource. Every PII-bearing table keyed to the
    subject (user id or their accounts) is included, all columns except secrets."""
    model_config = ConfigDict(extra="allow")
    user: dict
    profile: dict | None = None
    addresses: list[dict]
    payment_methods: list[dict]
    payment_mandates: list[dict] = []
    device_tokens: list[dict] = []
    orders: list[dict]
    invoices: list[dict]
    consents: list[dict]
    support_tickets: list[dict] = []
    ratings: list[dict] = []


def reject_nulls(*fields: str):
    """PATCH: a field may be omitted, but an explicit null on a NOT NULL column reached the
    DB (IntegrityError -> a misleading 409, or a TypeError 500 in a comparison). Reject it
    as a clean 422 instead."""

    def check(self):
        bad = [f for f in fields if f in self.model_fields_set and getattr(self, f) is None]
        if bad:
            raise ValueError(f"{', '.join(bad)} cannot be null")
        return self

    return model_validator(mode="after")(check)
