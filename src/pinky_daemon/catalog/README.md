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

Constructing a registry seeds and repairs only the immutable baseline, classifies
existing differences as operator-owned once, then applies the bundled document
in a separate transaction. Scripts that construct a registry apply the bundle to
that database too, including a scratch copy. An apply error is logged and retained
as best-effort status metadata; construction continues with the last good rows.

Operator changes through the registry methods or POST/DELETE `/models` own the
changed fields. New operator rows own all managed fields; automated discovery
rows remain roster-managed. Direct SQL edits after classification are unsupported
and do not record ownership. Context window and the 1M flag form one ownership
unit: editing or releasing either affects ownership of both.
Edits made while running an older release are not tracked; re-check them after upgrading.

Retiring a model is an operator action (DELETE `/models`). A roster never
deactivates an existing active model or inserts an inactive new model. Missing
rows remain unchanged; an unowned inactive row may be activated by a roster.

Apply accepts only a higher revision. A rollback publishes older values in a
new, higher revision. Dry-run previews bypass this gate, report `revision_gate`
as `accepted`, `equal`, or `lower`, and write no rows, status, or caches. Reports
count inserted/updated/unchanged and skipped rows; categories can overlap for a
partially owned row. Field counts distinguish individual writes and skips.
Release clears ownership and immediately reapplies the selected fields from the
exact saved document after validating its digest. It leaves global revision and
last-applied metadata unchanged; malformed saved data refuses the entire release.

Committed changes invalidate runtime price and 1M snapshots. Analytics reprices
recorded tokens using current runtime rates; the stored lifetime cost ledger is
unchanged. Static bundled fallback tables retain their import-time values.

In API mode, remote synchronization uses
`https://raw.githubusercontent.com/bradbrok/PinkyBot/main/src/pinky_daemon/catalog/models.json`
by default. Set `PINKY_MODEL_ROSTER_URL` to override it with an HTTPS URL on
`raw.githubusercontent.com` or `pinkybot.ai`. Configuration is captured when the
application is constructed. The first remote attempt follows listener readiness
and startup replay by 60 seconds; subsequent scheduled starts are roughly 24 hours
apart (23.5 hours plus up to 30 minutes of jitter). Manual requests do not reset
that schedule. Each API install makes one daily GET to the roster host unless
`PINKY_MODEL_ROSTER_SYNC` is set to `off`, `0`, or `false` (ignoring case and
surrounding whitespace). This switch disables both scheduled and manual fetches;
local bundled updates, status, and ownership release remain available.

GET `/models/roster` returns read-only sync status, configured URL, enabled flag,
and bundled revision. POST `/models/roster/sync` requires an explicit boolean
`dry_run`; previews change no rows, status, or caches. POST
`/models/roster/release` requires a full model `id` and managed `fields` list or
`"all"`, and uses the saved document without fetching. Isolated callers cannot
access this roster subtree. Existing `/models` operations keep their behavior.

Remote failures retain the last good roster, prices, and ownership. Fetches accept
only a validated single redirect, HTTP 200, identity encoding, and a bounded body
of at most 256 KiB. A 10-second async fetch deadline also covers executor wait;
late results are discarded and another fetch is refused while its worker is
pending. Cancelling a request or shutting down prevents later registry writes,
but cannot forcibly terminate an already-running network thread.
