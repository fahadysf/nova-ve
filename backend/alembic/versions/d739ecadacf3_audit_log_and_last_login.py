"""Audit log table and users.last_login (#297)

Revision ID: d739ecadacf3
Revises: 37fcfbd00d8a
Create Date: 2026-11-24 13:20:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd739ecadacf3'
down_revision: Union[str, None] = '37fcfbd00d8a'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'audit_log',
        # A plain BIGINT key: SQLAlchemy renders it BIGSERIAL on PostgreSQL.
        sa.Column('id', sa.BigInteger(), nullable=False),
        sa.Column('created_at', sa.BigInteger(), nullable=False),
        sa.Column('actor', sa.String(length=64), nullable=True),
        sa.Column('event', sa.String(length=64), nullable=False),
        sa.Column('category', sa.String(length=32), nullable=False),
        sa.Column('action', sa.String(length=32), nullable=False),
        sa.Column('outcome', sa.String(length=16), nullable=False),
        sa.Column('target_type', sa.String(length=32), nullable=True),
        sa.Column('target_id', sa.String(length=128), nullable=True),
        sa.Column('target_name', sa.String(length=255), nullable=True),
        sa.Column('lab_path', sa.Text(), nullable=True),
        sa.Column('lab_name', sa.String(length=255), nullable=True),
        sa.Column('ip', sa.String(length=64), nullable=True),
        sa.Column('detail', sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_audit_log_created_at'), 'audit_log', ['created_at'], unique=False)
    op.create_index(op.f('ix_audit_log_actor'), 'audit_log', ['actor'], unique=False)
    op.create_index(op.f('ix_audit_log_event'), 'audit_log', ['event'], unique=False)

    op.add_column('users', sa.Column('last_login', sa.BigInteger(), nullable=True))


def downgrade() -> None:
    op.drop_column('users', 'last_login')
    op.drop_index(op.f('ix_audit_log_event'), table_name='audit_log')
    op.drop_index(op.f('ix_audit_log_actor'), table_name='audit_log')
    op.drop_index(op.f('ix_audit_log_created_at'), table_name='audit_log')
    op.drop_table('audit_log')
