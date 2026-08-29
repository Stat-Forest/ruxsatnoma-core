"""Permission codes owned by admin, registered with the central registry at import.

Importing this module has the side effect of registration, so `router.py` (which
imports the constants) is enough to make `require_permission` accept them.
"""

from app.modules.auth.permissions import register

ORGANIZATIONS_MANAGE = "admin.organizations.manage"
CLASSIFIERS_MANAGE = "admin.classifiers.manage"
SETTINGS_MANAGE = "admin.settings.manage"
ANNOUNCEMENTS_MANAGE = "admin.announcements.manage"
INTEGRATIONS_VIEW = "admin.integrations.view"
INTEGRATIONS_MANAGE = "admin.integrations.manage"

register(
    {
        ORGANIZATIONS_MANAGE: "Create, edit and archive organizations (С23)",
        CLASSIFIERS_MANAGE: "Manage classifiers and their versioned items (С23)",
        SETTINGS_MANAGE: "Change runtime system settings (С23)",
        ANNOUNCEMENTS_MANAGE: "Create, publish and archive announcements (С23)",
        INTEGRATIONS_VIEW: "View the outbox, dead letters and integration log",
        INTEGRATIONS_MANAGE: "Requeue outbox messages, discard dead letters",
    }
)
