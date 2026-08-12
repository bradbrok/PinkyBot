# Deployment notes

The durable dream-receipts migration is one-way. Do not roll back below
PinkyBot 26.08.020 after migration: pre-gate binaries do not understand the v1
receipt ledger and may run stale watermark semantics beside it.
