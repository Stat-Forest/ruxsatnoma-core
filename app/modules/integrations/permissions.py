"""Permission codes owned by integrations; importing registers them.

`EIMZO_HEALTH` is registered but granted to no role in any seed migration --
`GET /eimzo/health` (Task 7) is reachable only through the `sys_admin`
superuser bypass in `auth.deps._authorize` (decision #41 ruling 2), the same
shape `applications.assign` uses. `tests/test_permissions_registry.py`'s
`UNGRANTED_BY_DESIGN` allowlist carries this code for exactly that reason.
"""

from app.modules.auth.permissions import register

EIMZO_HEALTH = "integrations.eimzo.health"

register(
    {
        EIMZO_HEALTH: (
            "View E-IMZO provider health -- VPN status and key expiry "
            "(sys_admin only, no role is granted this)"
        ),
    }
)
