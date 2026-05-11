"""CLI for pyrene-auth bootstrap operations.

Currently provides:
  - `pyrene-auth init-admin` — creates the first admin user. Reads
    `ADMIN_EMAIL` + `ADMIN_PASSWORD` env vars (or `--email` / `--password`
    flags) and inserts:
      * the default team (if missing)
      * the admin role (if missing)
      * the admin user (hashed password)
      * an admin grant in `user_team_roles`
    The command is idempotent — running it again with the same email is a
    no-op except for updating the password (admin recovery).
"""

from __future__ import annotations

import asyncio
import os
import sys
from uuid import uuid4

from sqlalchemy import select

from pyrene_auth.db import make_auth_engine, make_auth_session_factory
from pyrene_auth.hashing import hash_password
from pyrene_auth.models import Role, Team, User, UserTeamRole
from pyrene_auth.settings import AuthSettings


async def _init_admin(email: str, password: str) -> None:
    settings = AuthSettings()
    engine = make_auth_engine(settings)
    factory = make_auth_session_factory(engine)

    try:
        async with factory() as session:
            # Default team.
            team_q = await session.execute(select(Team).where(Team.name == "default"))
            team = team_q.scalar_one_or_none()
            if team is None:
                team = Team(id=uuid4(), name="default")
                session.add(team)
                await session.flush()

            # Admin role.
            role_q = await session.execute(select(Role).where(Role.name == "admin"))
            admin_role = role_q.scalar_one_or_none()
            if admin_role is None:
                admin_role = Role(id=uuid4(), name="admin", description="Full access")
                session.add(admin_role)
                await session.flush()

            # User (insert-or-update password).
            user_q = await session.execute(select(User).where(User.email == email))
            user = user_q.scalar_one_or_none()
            if user is None:
                user = User(
                    id=uuid4(),
                    email=email,
                    password_hash=hash_password(password),
                )
                session.add(user)
                await session.flush()
            else:
                user.password_hash = hash_password(password)
                await session.flush()

            # Grant.
            grant_q = await session.execute(
                select(UserTeamRole).where(
                    UserTeamRole.user_id == user.id,
                    UserTeamRole.team_id == team.id,
                    UserTeamRole.role_id == admin_role.id,
                )
            )
            if grant_q.scalar_one_or_none() is None:
                session.add(
                    UserTeamRole(
                        user_id=user.id, team_id=team.id, role_id=admin_role.id
                    )
                )

            await session.commit()
            sys.stdout.write(
                f"admin user '{email}' initialised "
                f"(team_id={team.id}, role_id={admin_role.id})\n"
            )
    finally:
        await engine.dispose()


def app(argv: list[str] | None = None) -> int:
    """Entry point exposed as `pyrene-auth` console script.

    Hand-rolled arg parsing (typer would be overkill for one command;
    keeps the dep list short).
    """
    args = argv if argv is not None else sys.argv[1:]
    if not args or args[0] != "init-admin":
        sys.stderr.write("usage: pyrene-auth init-admin [--email EMAIL --password PW]\n")
        return 2

    email = os.environ.get("ADMIN_EMAIL")
    password = os.environ.get("ADMIN_PASSWORD")
    it = iter(args[1:])
    for token in it:
        if token == "--email":
            email = next(it, None)
        elif token == "--password":
            password = next(it, None)

    if not email or not password:
        sys.stderr.write(
            "ADMIN_EMAIL and ADMIN_PASSWORD must be set (env or --flags)\n"
        )
        return 2

    asyncio.run(_init_admin(email, password))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(app())
