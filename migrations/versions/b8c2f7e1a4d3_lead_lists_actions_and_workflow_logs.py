"""lead lists: actions model + workflow trigger logs

Adds the columns required to turn Lead Lists into a first-class object with
configurable Actions ("Assign a Lead", "Trigger a Workflow") and to allow the
assignment_logs table to record workflow-only events.

All changes are additive:
- New columns have safe server defaults so existing rows keep their behaviour.
- rotation_id on queues is relaxed to nullable so a list can be workflow-only.
- close_user_id on assignment_logs is relaxed so workflow-only events can log
  without a Close user.

Revision ID: b8c2f7e1a4d3
Revises: 1337bb89fe85
Create Date: 2026-05-12 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'b8c2f7e1a4d3'
down_revision = '1337bb89fe85'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('queues', schema=None) as batch_op:
        batch_op.add_column(sa.Column(
            'assign_enabled', sa.Boolean(),
            nullable=False, server_default=sa.text('true'),
        ))
        batch_op.add_column(sa.Column(
            'workflow_enabled', sa.Boolean(),
            nullable=False, server_default=sa.text('false'),
        ))
        batch_op.add_column(sa.Column('workflow_id', sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column('workflow_name', sa.String(length=255), nullable=True))
        batch_op.alter_column(
            'rotation_id',
            existing_type=sa.VARCHAR(length=20),
            nullable=True,
        )

    with op.batch_alter_table('assignment_logs', schema=None) as batch_op:
        batch_op.alter_column(
            'close_user_id',
            existing_type=sa.VARCHAR(length=64),
            nullable=True,
        )
        batch_op.add_column(sa.Column('workflow_id', sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column('workflow_name', sa.String(length=255), nullable=True))


def downgrade():
    with op.batch_alter_table('assignment_logs', schema=None) as batch_op:
        batch_op.drop_column('workflow_name')
        batch_op.drop_column('workflow_id')
        batch_op.alter_column(
            'close_user_id',
            existing_type=sa.VARCHAR(length=64),
            nullable=False,
        )

    with op.batch_alter_table('queues', schema=None) as batch_op:
        batch_op.alter_column(
            'rotation_id',
            existing_type=sa.VARCHAR(length=20),
            nullable=False,
        )
        batch_op.drop_column('workflow_name')
        batch_op.drop_column('workflow_id')
        batch_op.drop_column('workflow_enabled')
        batch_op.drop_column('assign_enabled')
