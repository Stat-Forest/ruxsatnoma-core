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
# Ruling #177 (stage 9): its own code rather than a reuse of NORMS_MANAGE —
# the dictionary has no approve/publish lifecycle of its own (unlike a
# `Norm`), so folding it into the norm-drafting permission would grant it to
# every gis_specialist without a matching grant for `central_admin`, which
# ruling #177 names explicitly ("editable by the leshoz for itself and by
# the central admin for anyone"). Zone-scoped through the SAME idiom
# `service._assert_norm_zone` already uses (`service._assert_organization_
# zone`): a zone-scoped actor may act only on their own organization, a
# zone-free one (central) on any.
ACTIVITY_SEASONS_MANAGE = "norms.seasons.manage"

register(
    {
        NORMS_MANAGE: "Create and edit draft norms for a contour (GIS/norms specialist)",
        NORMS_APPROVE: "Approve a norm submitted for review (executor_head — the leshoz head)",
        NORMS_PUBLISH: "Publish an approved norm (central office, VMQ 689)",
        TARIFFS_MANAGE: "Create and edit draft tariffs and rule parameters (maker)",
        TARIFFS_PUBLISH: "Publish a tariff or rule parameter (checker, central office)",
        ACTIVITY_SEASONS_MANAGE: (
            "Edit the organization x activity season/minimum-term dictionary "
            "(leshoz for itself, central admin for any)"
        ),
    }
)
