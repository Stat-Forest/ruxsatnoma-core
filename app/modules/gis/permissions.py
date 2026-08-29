"""Permission codes owned by gis; importing registers them (ruling 17)."""

from app.modules.auth.permissions import register

CONTOURS_MANAGE = "gis.contours.manage"
CONTOURS_APPROVE = "gis.contours.approve"
LAYERS_MANAGE = "gis.layers.manage"

register(
    {
        CONTOURS_MANAGE: "Create and edit contours, draft versions and imports (GIS specialist)",
        CONTOURS_APPROVE: "Approve and publish contour versions and import batches",
        LAYERS_MANAGE: "Maintain restriction/fire-ban layer objects and layer presentation",
    }
)
