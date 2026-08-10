"""Forest fund geometry.

Contours, map layers, occupancy checks and tiles.

Marked as a candidate for extraction into a separate service, so the
rules here are stricter: the public API accepts and returns plain types,
never ORM objects, and never counts on a transaction shared with the
caller.

One documented exception: the contour occupancy check runs in the same
transaction as application creation, otherwise two applications pass
the check at once.

Owns the geo database schema and touches no other module's tables,
not even for reading.

This file is the public API of the module: everything other modules are
allowed to call is declared here, and every other file in the package is
internal and may change without coordination. See architecture/modules.md.
"""
