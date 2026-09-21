"""
FastAPI dependencies for authentication and entity-scoped authorization.

Usage in an endpoint:

    @router.get("/reconciliation/{entity_id}/items")
    async def list_open_items(
        entity_id: uuid.UUID,
        user: User = Depends(require_permission(
            TreasuryModule.BANK_RECONCILIATION, TreasuryAction.VIEW
        )),
    ):
        ...

`require_permission` returns a dependency callable that:
  1. Resolves the current authenticated user from the bearer token.
  2. Loads that user's role assignments.
  3. Confirms at least one assignment grants (module, action) either
     GROUP_WIDE, or ENTITY-scoped to the entity_id path/query param
     (when present).

This is the entity-level + module/action authorization foundation
required by SECTION 5 / 14 of the spec.
"""
import uuid
from collections.abc import Callable

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import OAuth2PasswordBearer
from jose import JWTError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.security import decode_token
from app.db.session import get_db
from app.models.rbac import (
    EntityScopeType,
    Role,
    TreasuryAction,
    TreasuryModule,
    User,
    UserRoleAssignment,
)

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="/api/v1/auth/login", auto_error=False)


async def get_current_user(
    token: str | None = Depends(oauth2_scheme),
    db: AsyncSession = Depends(get_db),
) -> User:
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Could not validate credentials",
        headers={"WWW-Authenticate": "Bearer"},
    )
    if token is None:
        raise credentials_exception
    try:
        payload = decode_token(token)
        if payload.get("type") != "access":
            raise credentials_exception
        user_id = payload.get("sub")
        if user_id is None:
            raise credentials_exception
    except JWTError:
        raise credentials_exception

    result = await db.execute(
        select(User)
        .options(
            selectinload(User.role_assignments)
            .selectinload(UserRoleAssignment.role)
            .selectinload(Role.permissions)
        )
        .where(User.id == uuid.UUID(user_id), User.is_active.is_(True))
    )
    user = result.scalar_one_or_none()
    if user is None:
        raise credentials_exception
    return user


def _grants_permission(
    user: User, module: TreasuryModule, action: TreasuryAction, entity_id: uuid.UUID | None
) -> bool:
    if user.is_superuser:
        return True
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
            return True
        if (
            assignment.scope_type == EntityScopeType.ENTITY
            and entity_id is not None
            and assignment.legal_entity_id == entity_id
        ):
            return True
        # Entity-scoped assignment but no entity_id was resolvable from the
        # request: fail closed rather than silently granting group-wide access.
    return False


def require_permission(module: TreasuryModule, action: TreasuryAction) -> Callable:
    """
    Returns a FastAPI dependency enforcing (module, action) for the current
    user, scoped to an `entity_id` found in the request's path or query
    params (if any). Endpoints with no entity concept simply omit it and
    only GROUP_WIDE-scoped grants (or superuser) will pass.
    """

    async def dependency(
        request: Request,
        user: User = Depends(get_current_user),
    ) -> User:
        entity_id_raw = (
            request.path_params.get("entity_id")
            or request.path_params.get("legal_entity_id")
            or request.query_params.get("entity_id")
            or request.query_params.get("legal_entity_id")
        )
        entity_id = uuid.UUID(entity_id_raw) if entity_id_raw else None

        if not _grants_permission(user, module, action, entity_id):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Missing permission {module.value}:{action.value}"
                + (f" for entity {entity_id}" if entity_id else ""),
            )
        return user

    return dependency
