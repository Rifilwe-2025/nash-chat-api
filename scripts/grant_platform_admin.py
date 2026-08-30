"""Grant or revoke platform administrator, by email address.

**The only way this flag is ever set.** There is deliberately no endpoint: an API that grants
platform admin is an API that escalates to platform admin, and the only thing then standing between
one compromised tenant account and every other tenant's data is whatever check that endpoint
happened to make. A script needs database credentials and a shell on the deployment, which is a
meaningfully higher bar and the same one the operator already holds.

Usage::

    python scripts/grant_platform_admin.py ada@acme.test
    python scripts/grant_platform_admin.py ada@acme.test --revoke
    python scripts/grant_platform_admin.py --list

It writes to whatever ``DATABASE_URL`` points at, so run it where the deployment's environment is —
inside the API container, or with that variable set.

What the flag buys, so it is granted deliberately: every route under `/admin`, and the ability to
act as any tenant by sending `X-Tenant-Id` on any ordinary endpoint. It does **not** exempt the
holder from anything else; they are still an ordinary user of their own tenant everywhere else.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

from src.modules.tenants.domain.models import Tenant, User
from src.shared.database.engine import create_engine


async def _run(email: str | None, revoke: bool, listing: bool) -> int:
    engine = create_engine()
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)

    try:
        async with factory() as session:
            if listing:
                rows = await session.execute(
                    select(User.email, Tenant.name)
                    .join(Tenant, User.tenant_id == Tenant.id)
                    .where(User.is_platform_admin.is_(True))
                    .order_by(User.email)
                )
                admins = list(rows)
                if not admins:
                    print("No platform administrators are set.")
                    return 0
                print(f"{len(admins)} platform administrator(s):")
                for address, tenant_name in admins:
                    print(f"  {address}  (tenant: {tenant_name})")
                return 0

            assert email is not None
            user = (
                await session.execute(
                    select(User).where(func.lower(User.email) == email.strip().lower())
                )
            ).scalar_one_or_none()

            if user is None:
                print(f"No user with the email {email!r}.", file=sys.stderr)
                return 1

            if user.is_platform_admin == (not revoke):
                state = "is already" if not revoke else "is already not"
                print(f"{user.email} {state} a platform administrator. Nothing to do.")
                return 0

            user.is_platform_admin = not revoke
            await session.commit()

            verb = "revoked from" if revoke else "granted to"
            print(f"Platform administrator {verb} {user.email}.")
            return 0
    finally:
        await engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("email", nargs="?", help="The user to grant or revoke.")
    parser.add_argument(
        "--revoke", action="store_true", help="Take the flag away instead of granting it."
    )
    parser.add_argument(
        "--list", dest="listing", action="store_true", help="Show who currently holds it."
    )
    args = parser.parse_args()

    if not args.listing and not args.email:
        parser.error("give an email address, or --list")

    raise SystemExit(asyncio.run(_run(args.email, args.revoke, args.listing)))


if __name__ == "__main__":
    main()
