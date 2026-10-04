# Model catalog

`models.json` is the shipped source for bootstrap model rows, static pricing,
and the native 1M-context fallback. The Python parser validates the whole document
before returning any rows; invalid bundled data fails during import.
`models.schema.json` describes the structural contract for other consumers.

Prices are USD per million tokens. The `cached_input` field is the cache-read
price; the static rate dictionary exposes it as `cache_read`. Cache-write prices
are recorded separately in `cache_write_5m` and `cache_write_1h`. A provider without
a 5-minute/1-hour split puts its documented write tariff in both fields, or `0`
in both if no write tariff is published.

Alias rows mirror the underlying model's values. For example,
`gpt-daybreak-blue-latest` mirrors `gpt-5.6-sol` in revision 1. Update the alias and
the underlying model together when their values change. Equal-priced roster
models share one plain rate dictionary; consumers must treat these dictionaries
as read-only. Dictionary iteration order is not part of the catalog contract.

Five identifiers absent from the catalog retain historical pricing in
`pricing._LEGACY_RATES`: `claude-haiku-3-5`, `claude-opus-4`, `claude-opus-4-1`,
`claude-sonnet-4`, and `gpt-5.3-codex`. These entries are separate from roster-derived
rates. A roster row cannot claim an identifier owned by that legacy table.

To add a model, edit `models.json`, supply every required row and pricing field,
increment `revision`, and set `updated` to a valid `YYYY-MM-DD` date. Bare model ids
must be unique across providers. The document requires 1..500 models and must fit
within 256 KiB. Check the context window against `is_1m`, and update related alias
rows together. Validation rejects duplicate object keys, unknown fields, invalid
values, and empty model lists.

`models.baseline.json` is the immutable byte copy of revision 1. Do not edit it for
later roster revisions. `scripts/generate_model_roster.py` records how revision 1
was extracted from the pinned historical source; it is not used at runtime.
