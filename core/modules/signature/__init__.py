"""Electronic signatures.

Storage of signatures and of their verification results. Shares the
permit schema with the permit module: a signature is stored next to the
document it signs. Verification itself is performed by the integration
service through the E-IMZO adapter.

Owns the permit database schema and touches no other module's tables,
not even for reading.

This file is the public API of the module: everything other modules are
allowed to call is declared here, and every other file in the package is
internal and may change without coordination. See architecture/modules.md.
"""
