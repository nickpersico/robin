"""add error_logs table

Records user-facing errors (500s, and 404s hit by signed-in users) so support
can triage them from the /system dashboard instead of grepping server logs.

Revision ID: f4c1a7b2e9d0
Revises: d7e3c91a8f02
Create Date: 2026-08-31 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'f4c1a7b2e9d0'
down_revision = 'd7e3c91a8f02'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'error_logs',
        sa.Column('id', sa.String(length=20), nullable=False),
        sa.Column('occurred_at', sa.DateTime(), nullable=False),
        sa.Column('status_code', sa.Integer(), nullable=False),
        sa.Column('error_type', sa.String(length=128), nullable=True),
        sa.Column('error_message', sa.Text(), nullable=True),
        sa.Column('traceback', sa.Text(), nullable=True),
        sa.Column('method', sa.String(length=8), nullable=True),
        sa.Column('path', sa.String(length=512), nullable=True),
        sa.Column('endpoint', sa.String(length=128), nullable=True),
        sa.Column('referrer', sa.String(length=512), nullable=True),
        sa.Column('user_id', sa.String(length=20), nullable=True),
        sa.Column('user_email', sa.String(length=255), nullable=True),
        sa.Column('close_org_id', sa.String(length=64), nullable=True),
        sa.Column('user_agent', sa.String(length=512), nullable=True),
        sa.Column('remote_addr', sa.String(length=64), nullable=True),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], ),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('error_logs', schema=None) as batch_op:
        batch_op.create_index('ix_error_logs_occurred_at', ['occurred_at'], unique=False)
        batch_op.create_index('ix_error_logs_status_code', ['status_code'], unique=False)
        batch_op.create_index('ix_error_logs_path', ['path'], unique=False)
        batch_op.create_index('ix_error_logs_user_id', ['user_id'], unique=False)
        batch_op.create_index('ix_error_logs_user_email', ['user_email'], unique=False)
        batch_op.create_index('ix_error_logs_close_org_id', ['close_org_id'], unique=False)


def downgrade():
    with op.batch_alter_table('error_logs', schema=None) as batch_op:
        batch_op.drop_index('ix_error_logs_close_org_id')
        batch_op.drop_index('ix_error_logs_user_email')
        batch_op.drop_index('ix_error_logs_user_id')
        batch_op.drop_index('ix_error_logs_path')
        batch_op.drop_index('ix_error_logs_status_code')
        batch_op.drop_index('ix_error_logs_occurred_at')
    op.drop_table('error_logs')
