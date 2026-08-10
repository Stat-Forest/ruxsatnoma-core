"""Norms, tariffs and calculation.

The rule engine and its versioned inputs. The formulas of VMQ 689 and
VMQ 278 live here and in no other module. An issued permit stays bound
to the rule_version and input_snapshot it was calculated from, so a new
norm never recalculates it.

Owns the rules database schema and touches no other module's tables,
not even for reading.

This file is the public API of the module: everything other modules are
allowed to call is declared here, and every other file in the package is
internal and may change without coordination. See architecture/modules.md.
"""
