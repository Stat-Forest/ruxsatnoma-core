"""Permission code owned by dashboard; importing registers it (same idiom as
`app/modules/oversight/permissions.py`). Granted (migration 0028) to every
staff role `tz/03`'s matrix gives the `Dashboard` row to — every role except
`applicant`, zone-scoped per role by `app/core/abac.py::zone_filter`, never by
a second permission code."""

from app.modules.auth.permissions import register

DASHBOARD_VIEW = "dashboard.view"

register(
    {
        DASHBOARD_VIEW: "View KPI tiles and the territory-slice drill-down",
    }
)
