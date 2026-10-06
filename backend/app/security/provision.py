"""Provision or reset a customer account without exposing passwords in shell history."""

from __future__ import annotations

import argparse
import asyncio
import getpass
import uuid

from sqlalchemy import select

from backend.app.config import get_settings
from backend.app.database import Database
from backend.app.models import CustomerAccount
from backend.app.security.sessions import hash_password, normalize_username


async def provision_account(database: Database, username: str, customer_id: uuid.UUID, password: str) -> None:
    normalized = normalize_username(username)
    encoded = hash_password(password)
    async with database.session_factory() as session:
        existing = await session.scalar(
            select(CustomerAccount).where(CustomerAccount.username == normalized)
        )
        owner = await session.get(CustomerAccount, customer_id)
        if existing is not None and existing.customer_id != customer_id:
            raise ValueError("Username belongs to a different customer")
        if owner is not None and owner.username != normalized:
            raise ValueError("Customer already has a different username")
        if owner is None:
            session.add(CustomerAccount(
                customer_id=customer_id, username=normalized, password_hash=encoded
            ))
        else:
            owner.password_hash = encoded
            owner.is_active = True
            owner.session_version += 1
        await session.commit()


def main() -> None:
    parser = argparse.ArgumentParser(description="Create or reset a customer login")
    parser.add_argument("username")
    parser.add_argument("customer_id", type=uuid.UUID)
    args = parser.parse_args()
    password = getpass.getpass("Password (at least 12 characters): ")
    confirmation = getpass.getpass("Confirm password: ")
    if password != confirmation:
        parser.error("Passwords do not match")

    async def run() -> None:
        database = Database(get_settings().database_url)
        try:
            await provision_account(database, args.username, args.customer_id, password)
        finally:
            await database.dispose()

    asyncio.run(run())
    print("Customer account ready")


if __name__ == "__main__":
    main()
