"""Permission codes owned by notifications; importing registers them (same idiom
as app/modules/admin/permissions.py)."""

from app.modules.auth.permissions import register

TEMPLATES_MANAGE = "notifications.templates.manage"

register({TEMPLATES_MANAGE: "Create, supersede and archive notification templates (С19)"})
