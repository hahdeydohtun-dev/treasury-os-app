"""
Centralized authorization helpers (Stage 2 Hardening pass).

Before this pass, every endpoint that needed entity-scoped authorization
called `_grants_permission(user, module, action, entity_id)` directly,
which has two structural problems this module fixes:

1. It only ever checks ONE entity_id at a time, so a list/aggregate
   endpoint had no way to ask "which entities is this user authorized
   for?" and filter a query accordingly - it could only gate the whole
   request behind a single id, which meant either 403-ing entity-scoped
   users out of their own unfiltered list view, or (if a caller forgot
   the check entirely) returning unfiltered data.
2. A GROUP_WIDE `UserRoleAssignment` was treated as unconditionally
   authorizing every entity in the system, never checking
   `UserRoleAssignment.group_id` against the entity's actual group. A
   user scoped GROUP_WIDE to Group 1 could therefore see Group 2's data
   through any endpoint that only checked "is this GROUP_WIDE" without
   checking "for which group".

`AuthorizedScope` + `get_authorized_scope` replace direct `_grants_permission`
calls in list/aggregate/export endpoints; `assert_entity_access` /
`assert_record_belongs_to_authorized_entity` replace direct
`_grants_permission` calls in single-record create/detail/update
endpoints. Both are built on the same underlying `UserRoleAssignment` /
`Permission` data - this is not a second competing authorization model,
it is `_grants_permission`'s logic made scope-aware and reusable.

A `UserRoleAssignment` with `scope_type=GROUP_WIDE` and `group_id=None` is
treated as truly unrestricted (system-wide) access - this preserves
existing test fixtures and any already-provisioned "super treasury admin"
style assignment that predates group-scoping. A `GROUP_WIDE` assignment
with `group_id` set is scoped to that group's entities only - the fix for
the cross-group leak.
"""
import uuid
from dataclasses import dataclass, field

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.entity import LegalEntity
from app.models.rbac import EntityScopeType, TreasuryAction, TreasuryModule, User


@dataclass
class AuthorizedScope:
    unrestricted: bool = False
    entity_ids: set = field(default_factory=set)
    group_ids: set = field(default_factory=set)

    @property
    def is_empty(self) -> bool:
        return not self.unrestricted and not self.entity_ids and not self.group_ids

    def allows_entity(self, entity_id, entity_group_id=None) -> bool:
        if self.unrestricted:
            return True
        if entity_id is not None and entity_id in self.entity_ids:
            return True
        if entity_group_id is not None and entity_group_id in self.group_ids:
            return True
        return False

    def allows_group(self, group_id) -> bool:
        if self.unrestricted:
            return True
        return group_id is not None and group_id in self.group_ids


async def get_authorized_scope(
    db: AsyncSession, user: User, module: TreasuryModule, action: TreasuryAction
) -> AuthorizedScope:
    if user.is_superuser:
        return AuthorizedScope(unrestricted=True)

    scope = AuthorizedScope()
    for assignment in user.role_assignments:
        if not assignment.is_active:
            continue
        has_perm = any(
            perm.module == module and perm.action == action
            for perm in assignment.role.permissions
        )
        if not has_perm:
            continue

        if assignment.scope_type == EntityScopeType.GROUP_WIDE:
            if assignment.group_id is None:
                scope.unrestricted = True
            else:
                scope.group_ids.add(assignment.group_id)
        elif assignment.scope_type == EntityScopeType.ENTITY and assignment.legal_entity_id:
            scope.entity_ids.add(assignment.legal_entity_id)

    return scope


async def assert_entity_access(
    db: AsyncSession,
    user: User,
    module: TreasuryModule,
    action: TreasuryAction,
    entity_id: uuid.UUID | None,
) -> LegalEntity | None:
    """
    For single-record create/detail/update/delete endpoints. Verifies the
    user is authorized for `entity_id` under (module, action), checking
    both direct entity-scoped grants and group-wide grants against the
    entity's real group membership. Raises 403/404 (never leaking whether
    the entity exists to an unauthorized caller) if not authorized.
    """
    scope = await get_authorized_scope(db, user, module, action)

    entity = None
    entity_group_id = None
    if entity_id is not None:
        entity = await db.get(LegalEntity, entity_id)
        entity_group_id = entity.group_id if entity else None

    if entity_id is not None and entity is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Entity not found")

    if not scope.allows_entity(entity_id, entity_group_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="No permission for this entity.",
        )
    return entity


async def apply_entity_scope(db: AsyncSession, stmt, entity_id_column, scope: AuthorizedScope, group_id_column=None):
    """
    Applies `scope` as a WHERE clause on `stmt`. Covers both shapes of
    row a scope-checked model like Forecast can have:
      - an entity-owned row (entity_id_column set) - authorized if that
        entity is directly granted OR its OWN group is one of the
        caller's authorized groups (a GROUP_WIDE grant covers every
        entity in that group, not just null-entity consolidated rows);
      - a group-wide row (entity_id_column is NULL, group_id_column set)
        - authorized only if that specific group is in the caller's
          authorized groups.
    An empty, non-unrestricted scope filters to zero rows rather than
    being skipped - "no authorization" must mean "no rows", never
    "unfiltered".
    """
    from sqlalchemy import and_, false, or_

    if scope.unrestricted:
        return stmt

    entity_ids = set(scope.entity_ids)
    if scope.group_ids:
        result = await db.execute(
            select(LegalEntity.id).where(LegalEntity.group_id.in_(scope.group_ids))
        )
        entity_ids.update(row[0] for row in result.all())

    conditions = []
    if entity_ids:
        conditions.append(entity_id_column.in_(entity_ids))
    if scope.group_ids and group_id_column is not None:
        conditions.append(and_(entity_id_column.is_(None), group_id_column.in_(scope.group_ids)))

    if not conditions:
        return stmt.where(false())
    return stmt.where(or_(*conditions))


async def resolve_scope_entity_ids(db: AsyncSession, scope: AuthorizedScope):
    """
    For models where every row has a non-null legal_entity_id (the common
    case: TreasuryTransaction, BankAccount, ExpectedCollection, etc. -
    unlike Forecast, which supports a null legal_entity_id for group-wide
    rows), a GROUP_WIDE scope must be translated into the concrete set of
    entity ids under that group before it can drive a plain `.in_()`
    filter. Returns "ALL" (unrestricted - skip filtering) or a concrete
    set[UUID] (possibly empty, meaning "no rows").
    """
    if scope.unrestricted:
        return "ALL"

    entity_ids = set(scope.entity_ids)
    if scope.group_ids:
        result = await db.execute(
            select(LegalEntity.id).where(LegalEntity.group_id.in_(scope.group_ids))
        )
        entity_ids.update(row[0] for row in result.all())
    return entity_ids


def apply_resolved_entity_scope(stmt, entity_id_column, resolved_entity_ids):
    """Applies the result of `resolve_scope_entity_ids` as a WHERE clause."""
    from sqlalchemy import false

    if resolved_entity_ids == "ALL":
        return stmt
    if not resolved_entity_ids:
        return stmt.where(false())
    return stmt.where(entity_id_column.in_(resolved_entity_ids))


async def resolve_scope_group_ids(db: AsyncSession, scope: AuthorizedScope):
    """
    The reverse of `resolve_scope_entity_ids`: for Group-level list/detail
    endpoints, a user needs to see the parent Group of any entity they're
    individually authorized for (for context/breadcrumbs), in addition to
    any group they hold outright GROUP_WIDE authorization over. Returns
    "ALL" or a concrete set[UUID] of authorized group ids.
    """
    if scope.unrestricted:
        return "ALL"

    group_ids = set(scope.group_ids)
    if scope.entity_ids:
        result = await db.execute(
            select(LegalEntity.group_id).where(LegalEntity.id.in_(scope.entity_ids))
        )
        group_ids.update(row[0] for row in result.all())
    return group_ids


async def assert_record_belongs_to_authorized_entity(
    db: AsyncSession,
    user: User,
    module: TreasuryModule,
    action: TreasuryAction,
    record_entity_id: uuid.UUID | None,
    record_group_id: uuid.UUID | None = None,
) -> None:
    """
    For detail endpoints on a record already loaded by id (e.g. a
    Forecast, a BankAccount): verifies the record's OWN entity/group
    matches the caller's authorized scope. Use this instead of re-deriving
    entity_id from the request query string - the request's query params
    are never the security boundary, the record's actual data is.
    """
    scope = await get_authorized_scope(db, user, module, action)
    if record_entity_id is not None:
        entity = await db.get(LegalEntity, record_entity_id)
        entity_group_id = entity.group_id if entity else None
        if scope.allows_entity(record_entity_id, entity_group_id):
            return
    elif record_group_id is not None and scope.allows_group(record_group_id) or record_entity_id is None and record_group_id is None and scope.unrestricted:
        return

    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Not authorized for this record.")
