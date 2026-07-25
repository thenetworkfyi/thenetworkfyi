"""Provision a least-privilege pg_monitor role for OTel metrics scraping.

Revision ID: 018
Revises: 017
Create Date: 2026-07-25
"""

from typing import Sequence, Union

from alembic import op

from thenetwork.settings import get_settings

revision: str = "018"
down_revision: Union[str, None] = "017"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _quote_ident(name: str) -> str:
    return '"' + name.replace('"', '""') + '"'


def _quote_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def upgrade() -> None:
    settings = get_settings()
    user_ident = _quote_ident(settings.postgres_monitor_user)
    user_literal = _quote_literal(settings.postgres_monitor_user)
    password_literal = _quote_literal(settings.postgres_monitor_password)
    db_ident = _quote_ident(settings.postgres_db)

    # This role is distinct from the application POSTGRES_USER: it is granted
    # only the built-in read-only pg_monitor role plus CONNECT, so the OTel
    # collector's postgresql receiver never holds application data privileges.
    op.execute(
        f"""
        DO $$
        BEGIN
            IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = {user_literal}) THEN
                CREATE ROLE {user_ident} LOGIN PASSWORD {password_literal};
            ELSE
                ALTER ROLE {user_ident} LOGIN PASSWORD {password_literal};
            END IF;
        END
        $$;
        """
    )
    op.execute(f"GRANT pg_monitor TO {user_ident};")
    op.execute(f"GRANT CONNECT ON DATABASE {db_ident} TO {user_ident};")


def downgrade() -> None:
    settings = get_settings()
    user_ident = _quote_ident(settings.postgres_monitor_user)
    op.execute(f"REVOKE pg_monitor FROM {user_ident};")
    op.execute(f"DROP ROLE IF EXISTS {user_ident};")
