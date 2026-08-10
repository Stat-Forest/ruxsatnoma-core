"""Issued permits.

The permit aggregate, document templates and QR verification.

Owns the permit database schema and touches no other module's tables,
not even for reading.

This file is the public API of the module: everything other modules are
allowed to call is declared here, and every other file in the package is
internal and may change without coordination. See architecture/modules.md.
"""
