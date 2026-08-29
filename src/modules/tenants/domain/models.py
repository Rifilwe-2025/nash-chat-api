"""Tenant and user tables (spec §7).

A tenant is the isolation boundary for every other table in the system: agents, knowledge bases, and
conversations all hang off ``tenant.id``. Users belong to exactly one tenant — in-tenant team
accounts are out of scope for v1.
"""

from __future__ import annotations

import enum

from sqlalchemy import String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.shared.database.base_model import BaseModel, TenantScopedModel, enum_column


class TenantPlan(str, enum.Enum):
    FREE = "free"
    STARTER = "starter"
    PRO = "pro"


class UserRole(str, enum.Enum):
    OWNER = "owner"
    MEMBER = "member"


class Tenant(BaseModel):
    __tablename__ = "tenant"

    name: Mapped[str] = mapped_column(String(255), nullable=False)
    plan: Mapped[TenantPlan] = mapped_column(
        enum_column(TenantPlan, "tenant_plan"),
        nullable=False,
        default=TenantPlan.FREE,
        server_default=TenantPlan.FREE.value,
    )

    users: Mapped[list[User]] = relationship(
        back_populates="tenant",
        cascade="all, delete-orphan",
        lazy="selectin",
    )


class User(TenantScopedModel):
    """``tenant_id`` comes from :class:`TenantScopedModel` — see spec §5.7."""

    __tablename__ = "user"
    __table_args__ = (UniqueConstraint("email", name="uq_user_email"),)

    email: Mapped[str] = mapped_column(String(320), nullable=False)
    full_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    password_hash: Mapped[str | None] = mapped_column(String(255), nullable=True)
    role: Mapped[UserRole] = mapped_column(
        enum_column(UserRole, "user_role"),
        nullable=False,
        default=UserRole.OWNER,
        server_default=UserRole.OWNER.value,
    )

    tenant: Mapped[Tenant] = relationship(back_populates="users")
