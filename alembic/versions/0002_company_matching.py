"""Company matching: automatic links are stored (with their reason and score) and people's answers are kept.

* ``company_aliases.source`` — "manual" or "auto"; ``company_aliases.score`` — the linker's confidence.
* ``company_match_decisions`` — "different" (never suggest this pair again) and "not_managed" (don't list
  this name as unmatched).

Revision ID: 0002_company_matching
Revises: 0001_baseline
Create Date: 2026-10-13
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0002_company_matching"
down_revision: str | None = "0001_baseline"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table('company_match_decisions',
    sa.Column('id', sa.String(length=36), nullable=False),
    sa.Column('name', sa.String(length=512), nullable=False),
    sa.Column('target', sa.String(length=512), server_default='', nullable=False),
    sa.Column('decision', sa.String(length=16), nullable=False),
    sa.Column('decided_by', sa.String(length=256), server_default='', nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('name', 'target', 'decision', name='uq_company_match_decision')
    )
    with op.batch_alter_table('company_match_decisions', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_company_match_decisions_name'), ['name'], unique=False)

    with op.batch_alter_table('company_aliases', schema=None) as batch_op:
        batch_op.add_column(sa.Column('source', sa.String(length=16), server_default='manual', nullable=False))
        batch_op.add_column(sa.Column('score', sa.Integer(), nullable=True))



def downgrade() -> None:
    with op.batch_alter_table('company_aliases', schema=None) as batch_op:
        batch_op.drop_column('score')
        batch_op.drop_column('source')

    with op.batch_alter_table('company_match_decisions', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_company_match_decisions_name'))

    op.drop_table('company_match_decisions')
