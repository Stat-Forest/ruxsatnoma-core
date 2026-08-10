"""Domain modules.

Eleven bounded contexts, one package per database schema. A module talks to a
neighbour only through that neighbour's public API, never through its schema
and never through its internal files. The dependency matrix is in
architecture/modules.md and is enforced by the contracts in setup.cfg.
"""
