"""Core service of the Ruxsatnoma electronic forest permit system.

A modular monolith: one process, eleven domain packages and three shared ones.
Every domain package owns exactly one database schema and exposes its public
API through its own ``__init__.py``. Module boundaries are described in
architecture/modules.md and enforced by import-linter.
"""
