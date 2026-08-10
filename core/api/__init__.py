"""REST controllers, middleware and error handling.

The only package allowed to depend on every domain module. It translates HTTP
into calls to module public APIs and domain errors into ERR-* responses; it
holds no business rules of its own.
"""
