"""Role names: stored roles become Reader / Editor / Approver / Admin.

``MSPTools.User`` → ``Reader``, ``MSPTools.Editor`` → ``Editor``, ``MSPTools.Approver`` → ``Approver``,
``MSPTools.Admin`` → ``Admin``. Each role includes the ones before it, so lists collapse to the highest one held. Old names are still
accepted when they arrive (sign-in claims, older tokens), so this only tidies what is stored.

Revision ID: 0003_three_roles
Revises: 0002_company_matching
Create Date: 2026-10-13
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0003_three_roles"
down_revision: str | None = "0002_company_matching"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_MAP = {"MSPTools.User": "Reader", "MSPTools.Editor": "Editor", "MSPTools.Approver": "Approver",
        "MSPTools.Admin": "Admin", "Reader": "Reader", "Editor": "Editor", "Approver": "Approver", "Admin": "Admin"}
_RANK = {"Reader": 1, "Editor": 2, "Approver": 3, "Admin": 4}
_TABLES = (("local_users", "username", "roles"), ("api_keys", "id", "roles"), ("user_sessions", "id_hash", "roles"),
           ("oauth_tokens", "token_hash", "user_roles"))


def _collapse(roles: object) -> list[str] | None:
    if not isinstance(roles, list):
        return None
    mapped = [_MAP[r] for r in roles if isinstance(r, str) and r in _MAP]
    return [max(mapped, key=_RANK.__getitem__)] if mapped else []


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    for table, key, column in _TABLES:
        if not insp.has_table(table) or column not in {c["name"] for c in insp.get_columns(table)}:
            continue
        t = sa.table(table, sa.column(key, sa.String), sa.column(column, sa.JSON))
        for row_key, roles in bind.execute(sa.select(t.c[key], t.c[column])).all():
            new = _collapse(roles)
            if new is not None and new != roles:
                bind.execute(sa.update(t).where(t.c[key] == row_key).values({column: new}))


def downgrade() -> None:
    back = {"Reader": ["MSPTools.User"], "Editor": ["MSPTools.User", "MSPTools.Editor"],
            "Approver": ["MSPTools.User", "MSPTools.Editor", "MSPTools.Approver"],
            "Admin": ["MSPTools.User", "MSPTools.Editor", "MSPTools.Approver", "MSPTools.Admin"]}
    bind = op.get_bind()
    insp = sa.inspect(bind)
    for table, key, column in _TABLES:
        if not insp.has_table(table) or column not in {c["name"] for c in insp.get_columns(table)}:
            continue
        t = sa.table(table, sa.column(key, sa.String), sa.column(column, sa.JSON))
        for row_key, roles in bind.execute(sa.select(t.c[key], t.c[column])).all():
            if isinstance(roles, list) and roles and roles[0] in back:
                bind.execute(sa.update(t).where(t.c[key] == row_key).values({column: back[roles[0]]}))
