import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.authorization import (
    apply_resolved_entity_scope,
    assert_entity_access,
    get_authorized_scope,
    resolve_scope_entity_ids,
    resolve_scope_group_ids,
)
from app.auth.dependencies import get_current_user, require_permission
from app.db.session import get_db
from app.models.entity import Group, LegalEntity
from app.models.rbac import TreasuryAction, TreasuryModule, User
from app.schemas.entity import GroupCreate, GroupOut, LegalEntityCreate, LegalEntityOut
from app.services.audit_service import record_audit_event

router = APIRouter(tags=["entities"])

MODULE = TreasuryModule.GROUP_ENTITY


@router.post("/groups", response_model=GroupOut, status_code=status.HTTP_201_CREATED)
async def create_group(
    payload: GroupCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_permission(MODULE, TreasuryAction.CREATE)),
) -> Group:
    group = Group(**payload.model_dump())
    db.add(group)
    await db.flush()
    await record_audit_event(
        db,
        module=MODULE.value,
        action="CREATE",
        record_type="Group",
        record_id=str(group.id),
        user_id=user.id,
        group_id=group.id,
        new_value=payload.model_dump(mode="json"),
    )
    await db.commit()
    await db.refresh(group)
    return group


@router.get("/groups", response_model=list[GroupOut])
async def list_groups(
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[Group]:
    """
    Server-side scoping (Stage 2 Hardening): a user only sees groups they
    hold GROUP_WIDE authorization over, plus the parent group(s) of any
    entity they're individually authorized for - never every group in
    the system merely because they can view *some* group's entities.
    """
    scope = await get_authorized_scope(db, user, MODULE, TreasuryAction.VIEW)
    if scope.is_empty:
        raise HTTPException(status_code=403, detail="No GROUP_ENTITY:VIEW permission.")

    resolved_group_ids = await resolve_scope_group_ids(db, scope)
    stmt = select(Group).where(Group.is_active.is_(True))
    stmt = apply_resolved_entity_scope(stmt, Group.id, resolved_group_ids)
    result = await db.execute(stmt)
    return list(result.scalars().all())


@router.get("/groups/{group_id}", response_model=GroupOut)
async def get_group(
    group_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Group:
    group = await db.get(Group, group_id)
    if group is None:
        raise HTTPException(status_code=404, detail="Group not found")

    scope = await get_authorized_scope(db, user, MODULE, TreasuryAction.VIEW)
    resolved_group_ids = await resolve_scope_group_ids(db, scope)
    if resolved_group_ids != "ALL" and group_id not in resolved_group_ids:
        raise HTTPException(status_code=403, detail="No permission for this group.")
    return group


@router.post(
    "/legal-entities", response_model=LegalEntityOut, status_code=status.HTTP_201_CREATED
)
async def create_legal_entity(
    payload: LegalEntityCreate,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(require_permission(MODULE, TreasuryAction.CREATE)),
) -> LegalEntity:
    group = await db.get(Group, payload.group_id)
    if group is None:
        raise HTTPException(status_code=404, detail="Parent group not found")

    legal_entity = LegalEntity(**payload.model_dump())
    db.add(legal_entity)
    await db.flush()
    await record_audit_event(
        db,
        module=MODULE.value,
        action="CREATE",
        record_type="LegalEntity",
        record_id=str(legal_entity.id),
        user_id=user.id,
        group_id=group.id,
        legal_entity_id=legal_entity.id,
        new_value=payload.model_dump(mode="json"),
    )
    await db.commit()
    await db.refresh(legal_entity)
    return legal_entity


@router.get("/legal-entities", response_model=list[LegalEntityOut])
async def list_legal_entities(
    group_id: uuid.UUID | None = None,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[LegalEntity]:
    scope = await get_authorized_scope(db, user, MODULE, TreasuryAction.VIEW)
    if scope.is_empty:
        raise HTTPException(status_code=403, detail="No GROUP_ENTITY:VIEW permission.")

    resolved_ids = await resolve_scope_entity_ids(db, scope)
    stmt = select(LegalEntity).where(LegalEntity.is_active.is_(True))
    stmt = apply_resolved_entity_scope(stmt, LegalEntity.id, resolved_ids)
    if group_id is not None:
        stmt = stmt.where(LegalEntity.group_id == group_id)
    result = await db.execute(stmt)
    return list(result.scalars().all())


@router.get("/legal-entities/{entity_id}", response_model=LegalEntityOut)
async def get_legal_entity(
    entity_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    user: User = Depends(get_current_user),
) -> LegalEntity:
    legal_entity = await db.get(LegalEntity, entity_id)
    if legal_entity is None:
        raise HTTPException(status_code=404, detail="Legal entity not found")
    await assert_entity_access(db, user, MODULE, TreasuryAction.VIEW, entity_id)
    return legal_entity
