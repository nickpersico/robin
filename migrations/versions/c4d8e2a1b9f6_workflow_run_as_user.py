"""lead lists: workflow run-as user

Adds two columns to track which Close user a Workflow trigger should run as
(used for sender_account_id/name/email when Robin creates the sequence
subscription). Both columns are additive and nullable — a NULL value means
"use the assigned rotation member" (only valid when assign is enabled).

Revision ID: c4d8e2a1b9f6
Revises: b8c2f7e1a4d3
Create Date: 2026-05-12 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = 'c4d8e2a1b9f6'
down_revision = 'b8c2f7e1a4d3'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('queues', schema=None) as batch_op:
        batch_op.add_column(sa.Column('workflow_run_as_user_id', sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column('workflow_run_as_user_name', sa.String(length=255), nullable=True))


def downgrade():
    with op.batch_alter_table('queues', schema=None) as batch_op:
        batch_op.drop_column('workflow_run_as_user_name')
        batch_op.drop_column('workflow_run_as_user_id')
