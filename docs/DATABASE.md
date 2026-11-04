# Persistence layer — SQLAlchemy 2.x notes & references

One page for maintainers: how the gateway's database layer is put together,
why it is shaped that way, and where the canonical documentation lives.
Nothing here is required reading to *use* OalgoPay — it's for changing it.

## Layout

| Path | Role |
|---|---|
| `app/models.py` | Declarative models — the single source of schema truth (plus `auth_attempts` as a plain `Table`, see below) |
| `app/db.py` | Every SQL statement in the gateway. Callers never touch sessions, engines, or statements |
| `migrations/` | Alembic revisions, run automatically on every boot (`db.connect()` → `upgrade head`) |

## Reads return ORM entities

Reads select the *class*, not the table, and come back as typed instances
(`Invoice`, `Payment`, …) via the `_entities()` / `_entity()` helpers:

```python
def get_invoice(inv_id: str) -> Invoice | None:
    return _entity(sa.select(Invoice).where(Invoice.id == inv_id))
```

Consumers use plain attribute access (`inv.status`, `p.txid`) everywhere —
templates, API serializers, the matcher. Scalar aggregates (settings,
counts, payment keys) stay scalars via `s.execute(...).scalar()` /
`.all()` on column selects.

Canonical references:
- [Unified tutorial (2.0 style)](https://docs.sqlalchemy.org/en/20/tutorial/index.html)
- [ORM Querying Guide — `select()`, `scalars()`](https://docs.sqlalchemy.org/en/20/orm/queryguide/index.html)

## Detached-by-design instances

Each accessor opens a short session, materializes entities, and returns
them **after** the session closes. Two properties make that safe here:

1. `sessionmaker(bind=_engine, expire_on_commit=False)` — attributes
   loaded before commit stay populated on the detached instance.
   ([`expire_on_commit` docs](https://docs.sqlalchemy.org/en/20/orm/session_api.html#sessionmaker.params.expire_on_commit))
2. **No relationships are declared** on any model. A detached object can
   only lazy-load something if a relationship exists to traverse; with
   zero relationships, `DetachedInstanceError` is impossible by
   construction. ([object states](https://docs.sqlalchemy.org/en/20/orm/session_state.html))

Rule of thumb if you ever add a relationship: either load it eagerly in
the same session (`selectinload`) or re-fetch inside the accessor — never
re-attach the caller's instance to a new session.

The matcher leans on this deliberately: `invoices.match_outputs()` treats
the `Invoice` entities it receives as **scratch state** — it mutates
`inv.paid_atomic` / `inv.status` locally, and the durable write goes back
through the returned field-dicts + `db.update_invoice()`. New payments are
constructed detached (`Payment(txid=…, …)`) and inserted via
`db.insert_payment(p)`. Nothing the matcher touches is ever session-bound.

## Writes stay Core

Inserts, updates, and deletes use the `Table` objects and Core statements,
not the ORM unit of work:

- Upserts need dialect constructs anyway: `_insert()` dispatches to
  SQLite/PostgreSQL `on_conflict_do_nothing()` / `on_conflict_do_update()`
  ([upsert recipes](https://docs.sqlalchemy.org/en/20/dialects/upsert.html)).
- `insert_invoice(inv: dict)` / `insert_payout(p: dict)` take plain field
  dicts because their only callers build them straight from API payloads;
  the projections (`_INVOICE_COLS`, `_PAYOUT_COLS`) pin the column set.
- `update_invoice(id, fields)` takes a field dict — that dict *is* the
  matcher/watcher write protocol (see `invoices.match_outputs`).

So: **ORM for reading (identity, typing, hydration), Core for writing
(upserts, bulk updates, no unit-of-work surprises)**.
([Core overview](https://docs.sqlalchemy.org/en/20/core/index.html))

## Joins return tuples, not labeled dicts

The delivery+endpoint joins return a `NamedTuple`, not a dict with
extra-label columns:

```python
class DeliveryWithEndpoint(NamedTuple):
    delivery: WebhookDelivery
    endpoint_url: str | None
    endpoint_secret: str | None
```

built from `sa.select(WebhookDelivery, WebhookEndpoint.url, …).join(...)`
— mixing entities and columns in one select yields rows of tuples, which
is cleaner to unpack than `dict(row._mapping)` with renamed keys.
([selecting entities + columns](https://docs.sqlalchemy.org/en/20/orm/queryguide/select.html#selecting-orm-entities-and-columns))

## `meta` vs `metadata`

`metadata` is reserved by `DeclarativeBase` (it holds the `MetaData`
registry), so the ORM attribute is `Invoice.meta` while the **column,
wire key, and admin code all stay `metadata`**:

```python
meta: Mapped[str] = mapped_column("metadata", Text, ...)
```

The one translation lives in `invoices.invoice_to_json()`:
`"metadata": _maybe_json(inv.meta)`. If you add another reader of this
column, use `.meta` and keep the external name `metadata`.
([explicit column naming](https://docs.sqlalchemy.org/en/20/orm/declarative_tables.html))

## Tables without a primary key

`auth_attempts` has no PK in the schema, and the ORM requires one — so it
is declared as a core `Table` in the same metadata and only ever inserted,
counted, and deleted through `db.py`. Don't promote it to a model without
also migrating a PK in.

## Domain behavior lives on the models

Small predicates belong on the class where the data lives — e.g.
`Invoice.is_cancellable()` (pending/seen) and `Invoice.is_expired(now)`
(pending/seen ∧ unpaid ∧ past `expires_at`), used by the cancel API and
the watcher's expiry sweep. Money-path *algorithms* (matching,
confirmation sweeping) stay module functions in `invoices.py` — pure,
testable, and independent of persistence.

## Migrations

- Boot always runs `upgrade head`; a stamp bridge translates the retired
  `schema_migrations` bookkeeping into an `alembic_version` row first
  (`db._stamp_legacy`).
- Revision ids are short and hand-set (`0001`, `0002`); keep the pattern.
- To add one: `uv run alembic revision -m "…"`, hand-write the upgrade
  (guard creates with an inspector check — see `0002`), then bump nothing
  else: boot and tests pick it up. The CLI resolves the database URL
  exactly like the app (`OALGOPAY_DATABASE_URL` → config → default
  SQLite), so `uv run alembic upgrade head` works standalone too.
- Rolling back to the pre-Alembic build is safe (two-way door) — see
  [INSTALL.md → Upgrading](INSTALL.md#upgrading).
- Reference: <https://alembic.sqlalchemy.org/>

## Quick API map

| You want | Use |
|---|---|
| A row as an object | `db.get_invoice(id)` / `db.list_invoices(...)` — returns `Invoice` / `list[Invoice]` |
| A joined shape | `db.pending_deliveries(now)` → `list[DeliveryWithEndpoint]` |
| An upsert | `db._insert(Table)` + `on_conflict_*` (already dialect-dispatched) |
| A new migration | `uv run alembic revision` (see above) |
| Entity → API JSON | `invoices.invoice_to_json(inv, payments, ...)` (handles `meta`→`metadata`) |
| Entity → pydantic | `model_config = ConfigDict(from_attributes=True)` (see `PayoutOut`) |
