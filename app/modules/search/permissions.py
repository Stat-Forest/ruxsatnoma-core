"""Permission codes owned by `search`; importing registers them (same idiom
as `app/modules/norms/permissions.py`). One code for the whole module —
plan ruling 3: granted to the roles that already hold both `applications`'
and `permits'` own view grants (migration `0029`), rather than gating each
`kind` on that domain's own permission code. С22's export routes (decision
#98) reuse this same code rather than adding a second one: an export shows
nothing `GET /search` does not already, so a separate code would only be a
second place for the two to drift apart."""

from app.modules.auth.permissions import register

SEARCH_USE = "search.use"

register({SEARCH_USE: "Cross-entity search and saved search profiles (zone-scoped)"})
