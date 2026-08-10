"""Long term archive.

Archived permits and forest tickets. Depends on nothing but shared.

Owns the arch database schema and touches no other module's tables,
not even for reading.

This file is the public API of the module: everything other modules are
allowed to call is declared here, and every other file in the package is
internal and may change without coordination. See architecture/modules.md.
"""
