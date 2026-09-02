"""Platform administration: the account list, and the lever over an account.

What an admin can do divides in two, and the split is what keeps this module small.

**Things that have no tenant context** are here: listing and searching accounts, seeing how big one
is, finding the account behind an email address, and enabling or disabling one. None of these can be
expressed as "a request scoped to one tenant", which is why they need a surface of their own.

**Everything else — the CRUD** — is not here. An admin creates an agent, uploads knowledge, reads a
transcript or revokes a key by *acting as* the tenant through the ordinary endpoints, which stay
tenant-scoped exactly as they are for the tenant themselves. A parallel admin API over every module
would be a second implementation of every rule those modules enforce, and the second one is always
the one that drifts.

The single write here goes through ``TenantService``: admin decides *that* an account should be
disabled, and the module that owns tenant rows is what changes one.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from src.modules.admin.domain.repositories import (
    PlatformTotals,
    PlatformUserRepository,
    TenantCounts,
    TenantDirectoryRepository,
)
from src.modules.tenants.domain.models import Tenant, TenantStatus, User
from src.modules.tenants.domain.services import TenantService
from src.shared.database.pagination import Page, PageRequest
from src.shared.exceptions import ConflictException, NotFoundException

logger = logging.getLogger("api.admin")

# Re-exported so the presentation layer can name what it renders without importing this module's
# repositories, which routers may not do (the layering rule in CLAUDE.md). The counts are part of
# this module's domain surface even though the queries behind them are not.
__all__ = ["AdminService", "PlatformTotals", "TenantCounts", "TenantDetail", "TenantSummary"]


@dataclass(frozen=True, slots=True)
class TenantSummary:
    tenant: Tenant
    counts: TenantCounts


@dataclass(frozen=True, slots=True)
class TenantDetail:
    tenant: Tenant
    counts: TenantCounts
    users: list[User]


class AdminService:
    """Every method assumes the caller has already been checked as platform staff.

    The check is a dependency rather than a constructor argument on purpose: a service that took a
    "is this an admin" flag could be constructed with the wrong one, whereas a route that forgets
    the dependency does not compile into an authenticated route at all.
    """

    def __init__(self, session: AsyncSession) -> None:
        self.session = session
        self.directory = TenantDirectoryRepository(session)
        self.users = PlatformUserRepository(session)
        self.tenants = TenantService(session)

    # -- reads ---------------------------------------------------------------

    async def list_tenants(
        self, page: PageRequest, search: str | None = None, status: TenantStatus | None = None
    ) -> tuple[Page[Tenant], dict[uuid.UUID, TenantCounts]]:
        """A page of accounts with their sizes.

        The counts come back as a map beside the page rather than folded into it, so the page
        stays the plain ``Page[Tenant]`` every listing returns and the caller renders what it wants.
        """
        result = await self.directory.search(page, term=search, status=status)
        counts = {tenant.id: await self.directory.counts_for(tenant.id) for tenant in result.items}
        return result, counts

    async def tenant_detail(self, tenant_id: uuid.UUID) -> TenantDetail:
        tenant = await self.tenants.get_tenant(tenant_id)
        return TenantDetail(
            tenant=tenant,
            counts=await self.directory.counts_for(tenant_id),
            users=await self.users.list_for_tenant(tenant_id),
        )

    async def find_account_by_email(self, email: str) -> TenantDetail:
        """The account behind an address — an operator's usual starting point.

        Somebody writes in, and the only thing identifying them is the address they signed up with.
        """
        user = await self.users.find_by_email(email)
        if user is None:
            raise NotFoundException("No account uses that email address.", code="USER_NOT_FOUND")
        return await self.tenant_detail(user.tenant_id)

    async def overview(self) -> PlatformTotals:
        return await self.directory.totals()

    async def platform_admin_exists(self) -> bool:
        """Whether the platform has an administrator at all.

        Asked at startup, before the bootstrap considers creating one. Unlike everything else on
        this service it is called by nobody who has been checked as staff — there is nobody to
        check yet, which is the situation it exists to detect — so it deliberately returns a bare
        boolean and discloses nothing about who the administrators are.
        """
        return await self.users.platform_admin_exists()

    # -- the lever -----------------------------------------------------------

    async def set_enabled(
        self, tenant_id: uuid.UUID, enabled: bool, note: str | None = None
    ) -> TenantSummary:
        """Enable or disable an account.

        Reversible and destructive of nothing: every row the tenant has stays exactly where it is,
        and the effect is entirely in what the authentication seams do next — nobody can sign in,
        the account's API keys are refused, and its agents answer on no channel.

        Logged at warning level with the reason, because "when did this account stop working, and
        who turned it off?" is the question that follows a disabled account by about a day.
        """
        status = TenantStatus.ACTIVE if enabled else TenantStatus.DISABLED
        tenant = await self.tenants.set_status(tenant_id, status, note)
        logger.warning(
            "tenant %s (%s) set to %s%s",
            tenant.id,
            tenant.name,
            status.value,
            f": {note}" if note else "",
        )
        # Returned with its counts, so the caller renders the same shape the listing does without
        # having to ask a second time — or reach past this service to do it.
        return TenantSummary(tenant=tenant, counts=await self.directory.counts_for(tenant.id))

    async def delete_tenant(self, tenant_id: uuid.UUID, confirmation: str) -> None:
        """Delete an account and everything in it. Irreversible.

        Guarded by having to type the account's name back. That is friction on purpose: every other
        destructive action in this API is scoped to one object a tenant already chose, while this
        one cascades through their agents, their knowledge, and every transcript they have — and the
        id in the URL of the account you are looking at is a very easy thing to paste twice.

        Disabling is almost always the right action instead, which is why the message says so.
        """
        tenant = await self.tenants.get_tenant(tenant_id)
        if confirmation.strip() != tenant.name:
            raise ConflictException(
                "Deleting an account requires confirming its exact name. Disabling it is "
                "reversible and is usually what is wanted instead.",
                code="TENANT_CONFIRMATION_MISMATCH",
            )

        logger.warning("deleting tenant %s (%s) and everything in it", tenant.id, tenant.name)
        await self.tenants.delete_tenant(tenant.id)
