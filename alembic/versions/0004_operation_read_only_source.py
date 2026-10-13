"""Operations remember where "read-only" came from, so a hand-made label can't let a write skip the write mode.

* ``operations.defined_read_only`` — the definition's own verdict (GET / GraphQL query / MCP readOnlyHint …).
* ``operations.read_only_template`` — marked read-only by a shipped template's list (ScreenConnect, ESET MSP …).

Existing rows: the verdict is worked out from the HTTP verb (GET/HEAD/OPTIONS/QUERY read; POST/PUT/PATCH/DELETE/
MUTATION write), from the name for SOAP, else the stored flag is kept. A write-verb operation that is marked
read-only is kept read-only only when its connection's template lists it; any other such label was made by hand
and is reset, so the call goes through the write mode again.

Revision ID: 0004_operation_read_only_source
Revises: 0003_three_roles
Create Date: 2026-10-13
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0004_operation_read_only_source"
down_revision: str | None = "0003_three_roles"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_READ_VERBS = {"GET", "HEAD", "OPTIONS", "QUERY"}
_WRITE_VERBS = {"POST", "PUT", "PATCH", "DELETE", "MUTATION"}


def _template_reads(template_id: object) -> set[tuple[str, str]]:
    if not template_id:
        return set()
    try:
        from app.services import connection_templates as ct

        return set(ct.read_only(str(template_id)))
    except Exception:  # noqa: BLE001 — template gone or renamed: nothing is vouched for
        return set()


def _looks_read_only(name: str) -> bool | None:
    try:
        from app.connections.base import looks_read_only

        return looks_read_only(name)
    except Exception:  # noqa: BLE001
        return None


def upgrade() -> None:
    with op.batch_alter_table('operations', schema=None) as batch_op:
        batch_op.add_column(sa.Column('defined_read_only', sa.Boolean(), server_default=sa.true(), nullable=False))
        batch_op.add_column(sa.Column('read_only_template', sa.Boolean(), server_default=sa.false(), nullable=False))

    bind = op.get_bind()
    conns = sa.table("connections", sa.column("id", sa.String), sa.column("kind", sa.String),
                     sa.column("settings", sa.JSON))
    ops = sa.table("operations", sa.column("id", sa.String), sa.column("connection_id", sa.String),
                   sa.column("method", sa.String), sa.column("target", sa.Text), sa.column("read_only", sa.Boolean),
                   sa.column("defined_read_only", sa.Boolean), sa.column("read_only_template", sa.Boolean))
    info = {cid: (kind, settings if isinstance(settings, dict) else {})
            for cid, kind, settings in bind.execute(sa.select(conns.c.id, conns.c.kind, conns.c.settings)).all()}
    reads_cache: dict[str, set[tuple[str, str]]] = {}
    for oid, cid, method, target, read_only in bind.execute(
            sa.select(ops.c.id, ops.c.connection_id, ops.c.method, ops.c.target, ops.c.read_only)).all():
        kind, settings = info.get(cid, ("", {}))
        verb = (method or "").upper()
        if verb in _READ_VERBS:
            defined = True
        elif verb in _WRITE_VERBS:
            defined = False
        elif kind == "soap" and (guess := _looks_read_only(target or "")) is not None:
            defined = guess
        else:
            defined = bool(read_only)
        template = False
        new_read_only = bool(read_only)
        if new_read_only and not defined:
            tid = str(settings.get("template") or "")
            reads = reads_cache.setdefault(tid, _template_reads(tid))
            template = (verb, target or "") in reads
            new_read_only = template
        if (defined, template, new_read_only) != (True, False, bool(read_only)):
            bind.execute(sa.update(ops).where(ops.c.id == oid).values(
                defined_read_only=defined, read_only_template=template, read_only=new_read_only))


def downgrade() -> None:
    with op.batch_alter_table('operations', schema=None) as batch_op:
        batch_op.drop_column('read_only_template')
        batch_op.drop_column('defined_read_only')
