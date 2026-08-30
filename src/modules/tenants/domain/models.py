"""Tenant and user tables (spec §7).

A tenant is the isolation boundary for every other table in the system: agents, knowledge bases, and
conversations all hang off ``tenant.id``. Users belong to exactly one tenant — in-tenant team
accounts are out of scope for v1.

Two columns here are the platform's own levers rather than a tenant's own data, and both are
deliberately blunt. ``tenant.status`` suspends an account without deleting anything; on
``user.is_platform_admin`` hangs every cross-tenant capability the admin module has. Neither is
reachable through a tenant-facing endpoint.
"""

from __future__ import annotations

import enum
from datetime import datetime

from sqlalchemy import Boolean, DateTime, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.shared.database.base_model import BaseModel, TenantScopedModel, enum_column


class TenantPlan(str, enum.Enum):
    """A label on the account, not a set of limits.

    The platform does not charge for use and enforces no plan ceilings — that was built and removed
    (see the plan's Phase 14). The column stays because it still says something useful about an
    account, and because nothing is gained by dropping a small varchar that harms nobody.
    """

    FREE = "free"
    STARTER = "starter"
    PRO = "pro"


class TenantStatus(str, enum.Enum):
    """Whether an account may be used at all.

    This is the lever the platform has over an account now that it does not bill for one. A
    ``DISABLED`` tenant keeps every row it has — agents, knowledge, transcripts — and simply stops
    working: nobody can sign in, and its agents answer on no channel. That is deliberately
    reversible, because the alternative when an account misbehaves would be deletion, and deletion
    of a support agent's history is not a decision to make in a hurry.

    Enforced at the two authentication seams and the one channel that has neither (WhatsApp's
    webhook), rather than in each endpoint — a status checked per route is a status somebody forgets
    to check.
    """

    ACTIVE = "active"
    DISABLED = "disabled"


class UserRole(str, enum.Enum):
    """The user's role *inside their own tenant*.

    Not to be confused with :attr:`User.is_platform_admin`, which is about the platform rather than
    about a tenant. The two are orthogonal on purpose: platform staff still belong to a tenant of
    their own, and being an owner of it says nothing about whether they may touch anybody else's.
    """

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
    status: Mapped[TenantStatus] = mapped_column(
        enum_column(TenantStatus, "tenant_status"),
        nullable=False,
        default=TenantStatus.ACTIVE,
        server_default=TenantStatus.ACTIVE.value,
        index=True,
    )
    # Why the account was disabled, for whoever finds it disabled later. Not shown to the tenant:
    # they are told their account is disabled and who to contact, not what a note about them says.
    status_note: Mapped[str | None] = mapped_column(String(500), nullable=True)
    status_changed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    @property
    def is_active(self) -> bool:
        return self.status is TenantStatus.ACTIVE

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
    # Platform staff. Grants every cross-tenant capability the admin module has, so it is set out
    # of band — by `scripts/grant_platform_admin.py` — and by no endpoint. An API that can grant
    # this is an API that can escalate to it.
    is_platform_admin: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )

    # Eagerly joined wherever the authentication path loads a user, so checking that their account
    # is still enabled costs no extra round trip on every request. See the repository.
    tenant: Mapped[Tenant] = relationship(back_populates="users")
