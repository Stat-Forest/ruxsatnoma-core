"""One substring-search predicate for every list route that takes a free-text
`q` (`GET /applications`, `GET /permits`, `GET /search`).

Lives in `core` because two level-3 modules and one level-5 reader share it,
and a module must never import another module's repo helper. The predicate
itself: `ILIKE '%needle%'` OR'd across the given columns, `unaccent`-wrapped
on both sides so a diacritic in either the query or the stored value does not
hide a match. A literal `%`/`_`/`\\` typed by the caller is escaped first so
it matches itself rather than acting as an ILIKE wildcard. `unaccent` is
installed by migration `0001`.
"""

from typing import Any

from sqlalchemy import func, or_


def text_filter(pattern: str, *columns: Any) -> Any:
    escaped = pattern.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    needle = func.unaccent(f"%{escaped}%")
    return or_(*(func.unaccent(col).ilike(needle) for col in columns))
