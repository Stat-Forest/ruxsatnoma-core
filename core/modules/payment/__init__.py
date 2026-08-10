"""Payment domain.

Invoices, reconciliation, allocation and refunds. Money is always
numeric(18,2) and Decimal, never float and never int.

Marked as a candidate for extraction into a separate service, so the
rules here are stricter: the public API accepts and returns plain types,
never ORM objects, and never counts on a transaction shared with the
caller.

Provider adapters (Payme, Click, Uzum, Paynet) live in the integration
service behind a single interface, not here.

Owns the pay database schema and touches no other module's tables,
not even for reading.

This file is the public API of the module: everything other modules are
allowed to call is declared here, and every other file in the package is
internal and may change without coordination. See architecture/modules.md.
"""
