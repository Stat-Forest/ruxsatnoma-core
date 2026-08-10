"""Oversight showcase.

The risk indicator detectors and the read only showcase for the
prosecutor role. Reads every other module through its public API and is
read by none of them, which is what makes it safe to serve from the
read_only connection pool. A new risk indicator is a new detector class,
not an edit to an existing one.

Owns no database schema of its own: it only reads what other modules
expose.

This file is the public API of the module: everything other modules are
allowed to call is declared here, and every other file in the package is
internal and may change without coordination. See architecture/modules.md.
"""
