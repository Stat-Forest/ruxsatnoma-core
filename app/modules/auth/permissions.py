"""Permission code registry (design/02: 'the function-code registry lives in code').

Each module OWNS its codes and registers them here at import time, the same way
audit actions belong to acting modules. 3.2a seeds only auth's own codes; the
matrix (tz/03 4-ilova) fills as modules land in 3.3+.
"""

PERMISSIONS: dict[str, str] = {
    "auth.users.manage": "Create/edit users, reset passwords (admin, stage 3.3)",
    "auth.sessions.revoke_any": "Revoke any user's sessions (admin, stage 3.3)",
}


def register(codes: dict[str, str]) -> None:
    """Called by other modules to add their permission codes."""
    for code, description in codes.items():
        if code in PERMISSIONS:
            raise ValueError(f"permission code already registered: {code}")
        PERMISSIONS[code] = description
