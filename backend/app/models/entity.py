"""
Group & Legal Entity domain models.

PRINCIPLE 1: Every financial object belongs to a legal entity.
A Group is the top-level consolidation unit; it contains one or more
Legal Entities. Business Unit is architecture-ready but not required
for the foundation stage.
"""
import uuid

from sqlalchemy import ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base_class import Base, SoftDeleteMixin, TimestampMixin, UUIDPKMixin


class Group(Base, UUIDPKMixin, TimestampMixin, SoftDeleteMixin):
    __tablename__ = "groups"

    name: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    code: Mapped[str] = mapped_column(String(50), nullable=False, unique=True)
    reporting_currency_code: Mapped[str] = mapped_column(
        String(3),
        ForeignKey("currencies.code"),
        nullable=False,
        comment="Group-level consolidation/reporting currency",
    )
    description: Mapped[str | None] = mapped_column(String(1000), nullable=True)

    legal_entities: Mapped[list["LegalEntity"]] = relationship(
        back_populates="group", cascade="all, delete-orphan"
    )


class LegalEntity(Base, UUIDPKMixin, TimestampMixin, SoftDeleteMixin):
    __tablename__ = "legal_entities"

    group_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("groups.id"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    code: Mapped[str] = mapped_column(String(50), nullable=False, unique=True)
    country: Mapped[str | None] = mapped_column(String(2), nullable=True, comment="ISO 3166-1 alpha-2")
    functional_currency_code: Mapped[str] = mapped_column(
        String(3), ForeignKey("currencies.code"), nullable=False
    )
    registration_number: Mapped[str | None] = mapped_column(String(100), nullable=True)
    tax_id: Mapped[str | None] = mapped_column(String(100), nullable=True)

    group: Mapped["Group"] = relationship(back_populates="legal_entities")
    business_units: Mapped[list["BusinessUnit"]] = relationship(
        back_populates="legal_entity", cascade="all, delete-orphan"
    )


class BusinessUnit(Base, UUIDPKMixin, TimestampMixin, SoftDeleteMixin):
    """
    Architecture-ready, optional sub-division of a Legal Entity.
    Not required for foundation-stage functionality, but modeled now so
    later modules (e.g. cost-center level reporting) don't require a
    breaking schema change.
    """
    __tablename__ = "business_units"

    legal_entity_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("legal_entities.id"), nullable=False, index=True
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    code: Mapped[str] = mapped_column(String(50), nullable=False)

    legal_entity: Mapped["LegalEntity"] = relationship(back_populates="business_units")
