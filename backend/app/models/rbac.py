"""
Role & Permission Administration foundation.

Supports the authorization model from SECTION 5 / 14 of the spec:
  MODULE x ACTION x ENTITY SCOPE

A Role is a named bundle of (module, action) Permissions.
A UserRoleAssignment binds a User to a Role within a scope:
  - group-wide (entity_id is null, scope = GROUP)
  - a specific legal entity (scope = ENTITY, entity_id set)

This is deliberately generic so future modules only need to add new
Module/Action enum values, not new tables.
"""
import uuid
from enum import Enum

from sqlalchemy import Boolean, ForeignKey, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base_class import Base, SoftDeleteMixin, TimestampMixin, UUIDPKMixin


class TreasuryModule(str, Enum):
    """One entry per module in SECTION 4. Extend as modules are built."""
    GROUP_ENTITY = "GROUP_ENTITY"
    BANKS_ACCOUNTS = "BANKS_ACCOUNTS"
    CASH_LIQUIDITY = "CASH_LIQUIDITY"
    FORECAST_13WK = "FORECAST_13WK"
    FACILITIES = "FACILITIES"
    PAYMENTS = "PAYMENTS"
    BANK_RECONCILIATION = "BANK_RECONCILIATION"
    INTERCOMPANY_RECONCILIATION = "INTERCOMPANY_RECONCILIATION"
    WORKING_CAPITAL = "WORKING_CAPITAL"
    INVESTMENTS = "INVESTMENTS"
    RISK_CONTROLS = "RISK_CONTROLS"
    KPIS = "KPIS"
    REPORTS = "REPORTS"
    TASKS_WORKFLOW = "TASKS_WORKFLOW"
    ADMINISTRATION = "ADMINISTRATION"
    EXCEL_DATA_HUB = "EXCEL_DATA_HUB"
    CURRENCY_FX = "CURRENCY_FX"
    AI_COPILOT = "AI_COPILOT"
    AUDIT = "AUDIT"


class TreasuryAction(str, Enum):
    VIEW = "VIEW"
    UPLOAD = "UPLOAD"
    IMPORT = "IMPORT"
    CREATE = "CREATE"
    EDIT = "EDIT"
    SUBMIT = "SUBMIT"
    INVESTIGATE = "INVESTIGATE"
    ASSIGN = "ASSIGN"
    COMMENT = "COMMENT"
    APPROVE = "APPROVE"
    EXECUTE = "EXECUTE"
    RESOLVE = "RESOLVE"
    CLOSE = "CLOSE"
    ADJUST = "ADJUST"
    EXPORT = "EXPORT"
    CONFIGURE = "CONFIGURE"
    PLACE = "PLACE"
    TERMINATE = "TERMINATE"
    ROLLOVER = "ROLLOVER"
    REBOOK = "REBOOK"


class EntityScopeType(str, Enum):
    GROUP_WIDE = "GROUP_WIDE"
    ENTITY = "ENTITY"


class User(Base, UUIDPKMixin, TimestampMixin, SoftDeleteMixin):
    __tablename__ = "users"

    email: Mapped[str] = mapped_column(String(255), nullable=False, unique=True, index=True)
    full_name: Mapped[str] = mapped_column(String(255), nullable=False)
    hashed_password: Mapped[str] = mapped_column(String(255), nullable=False)
    is_superuser: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    mfa_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    mfa_secret: Mapped[str | None] = mapped_column(String(255), nullable=True)

    role_assignments: Mapped[list["UserRoleAssignment"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


class Role(Base, UUIDPKMixin, TimestampMixin, SoftDeleteMixin):
    __tablename__ = "roles"

    name: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)
    description: Mapped[str | None] = mapped_column(String(500), nullable=True)
    is_system_role: Mapped[bool] = mapped_column(
        default=False, comment="System roles (e.g. Treasury Manager) are seeded, not user-created"
    )

    permissions: Mapped[list["Permission"]] = relationship(
        back_populates="role", cascade="all, delete-orphan"
    )


class Permission(Base, UUIDPKMixin, TimestampMixin):
    """A single (module, action) grant belonging to a Role."""
    __tablename__ = "permissions"
    __table_args__ = (
        UniqueConstraint("role_id", "module", "action", name="uq_role_module_action"),
    )

    role_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("roles.id"), nullable=False, index=True
    )
    module: Mapped[TreasuryModule] = mapped_column(nullable=False)
    action: Mapped[TreasuryAction] = mapped_column(nullable=False)

    role: Mapped["Role"] = relationship(back_populates="permissions")


class UserRoleAssignment(Base, UUIDPKMixin, TimestampMixin, SoftDeleteMixin):
    """
    Binds a User + Role to a scope. If scope_type is GROUP_WIDE, entity_id
    is null and the grant applies across all legal entities in the group.
    If scope_type is ENTITY, entity_id must be set and the grant applies
    only to that legal entity.
    """
    __tablename__ = "user_role_assignments"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id"), nullable=False, index=True
    )
    role_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("roles.id"), nullable=False, index=True
    )
    scope_type: Mapped[EntityScopeType] = mapped_column(nullable=False)
    legal_entity_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("legal_entities.id"), nullable=True, index=True
    )
    group_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("groups.id"), nullable=True, index=True
    )

    user: Mapped["User"] = relationship(back_populates="role_assignments")
    role: Mapped["Role"] = relationship()
