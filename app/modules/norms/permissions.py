"""Permission codes owned by norms; importing registers them (same idiom as
app/modules/gis/permissions.py). Publication of a norm sits with the central
office because VMQ 689 has forest-pasture norms approved by the head of the
forestry authority, not by a leshoz (ruling 16)."""

from app.modules.auth.permissions import register

NORMS_MANAGE = "norms.manage"
NORMS_APPROVE = "norms.approve"
NORMS_PUBLISH = "norms.publish"
TARIFFS_MANAGE = "norms.tariffs.manage"
TARIFFS_PUBLISH = "norms.tariffs.publish"

register(
    {
        NORMS_MANAGE: "Create and edit draft norms for a contour (GIS/norms specialist)",
        NORMS_APPROVE: "Approve a norm submitted for review (executor_head — the leshoz head)",
        NORMS_PUBLISH: "Publish an approved norm (central office, VMQ 689)",
        TARIFFS_MANAGE: "Create and edit draft tariffs and rule parameters (maker)",
        TARIFFS_PUBLISH: "Publish a tariff or rule parameter (checker, central office)",
    }
)
