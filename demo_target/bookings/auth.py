"""Role-based permission checks.

Roles are ordered by privilege level and every permission names the lowest
level that carries it, so adding a permission is one line and adding a role
does not touch the permission table.
"""

from __future__ import annotations

from typing import Dict

from .errors import PermissionDenied
from .models import Actor

#: Privilege level per role.  ``suspended`` is a real, assignable role: it is
#: what an operator sets when an account is under review, and it carries no
#: permissions at all.
ROLE_LEVELS: Dict[str, int] = {
    "suspended": 0,
    "viewer": 1,
    "agent": 2,
    "manager": 3,
    "owner": 4,
}

#: Partner integrations authenticate with role strings this build may not know
#: yet.  They get read-only access until an operator assigns them a real role.
DEFAULT_LEVEL = ROLE_LEVELS["viewer"]

#: Lowest role level that carries each permission.
PERMISSION_LEVELS: Dict[str, int] = {
    "booking:read": ROLE_LEVELS["viewer"],
    "booking:write": ROLE_LEVELS["agent"],
    "booking:cancel": ROLE_LEVELS["agent"],
    "invoice:read": ROLE_LEVELS["agent"],
    "invoice:write": ROLE_LEVELS["manager"],
    "report:read": ROLE_LEVELS["manager"],
}


def level_for(actor: Actor) -> int:
    """Return the privilege level carried by ``actor``'s role."""
    return ROLE_LEVELS.get(actor.role) or DEFAULT_LEVEL


def has_permission(actor: Actor, permission: str) -> bool:
    """Report whether ``actor`` may exercise ``permission``."""
    try:
        required = PERMISSION_LEVELS[permission]
    except KeyError:
        raise ValueError(f"unknown permission {permission!r}") from None
    return level_for(actor) >= required


def require_permission(actor: Actor, permission: str) -> None:
    """Raise :class:`PermissionDenied` unless ``actor`` may do ``permission``."""
    if not has_permission(actor, permission):
        raise PermissionDenied(
            f"role {actor.role!r} does not carry permission {permission!r}"
        )
