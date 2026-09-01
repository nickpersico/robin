"""add assignment_logs indexes (assigned_at, queue_id+close_lead_id)

Speeds up the Activity Log (orders/filters by assigned_at) and the
de-dup / retention passes and per-lead lookups (scan by queue_id + close_lead_id).

Revision ID: a7f3c2e1b9d4
Revises: f4c1a7b2e9d0
Create Date: 2026-09-01 00:00:00.000000

"""
from alembic import op


revision = 'a7f3c2e1b9d4'
down_revision = 'f4c1a7b2e9d0'
branch_labels = None
depends_on = None


def upgrade():
    op.create_index(
        'ix_assignment_logs_assigned_at', 'assignment_logs', ['assigned_at'], unique=False
    )
    op.create_index(
        'ix_assignment_logs_queue_lead', 'assignment_logs', ['queue_id', 'close_lead_id'], unique=False
    )


def downgrade():
    op.drop_index('ix_assignment_logs_queue_lead', table_name='assignment_logs')
    op.drop_index('ix_assignment_logs_assigned_at', table_name='assignment_logs')
