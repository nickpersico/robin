"""lead lists: workflow subscription id + denormalised close_org_id

Two additive changes:
1. assignment_logs gains workflow_subscription_id so the UI can deep-link
   a "Triggered workflow X" log entry to the actual workflow run in Close.
2. queues gains close_org_id (with backfill from rotations.close_org_id) so
   workflow-only Lead Lists — which have no rotation — are still scoped to a
   specific org. This closes the org-isolation gap that workflow-only lists
   currently rely on @login_required to paper over.

Both changes are nullable so existing rows are unaffected.

Revision ID: d7e3c91a8f02
Revises: c4d8e2a1b9f6
Create Date: 2026-05-13 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = 'd7e3c91a8f02'
down_revision = 'c4d8e2a1b9f6'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('assignment_logs', schema=None) as batch_op:
        batch_op.add_column(sa.Column('workflow_subscription_id', sa.String(length=64), nullable=True))

    with op.batch_alter_table('queues', schema=None) as batch_op:
        batch_op.add_column(sa.Column('close_org_id', sa.String(length=64), nullable=True))
        batch_op.create_index('ix_queues_close_org_id', ['close_org_id'])

    # Backfill: every existing queue has a rotation_id, so we can copy the
    # org from the linked rotation.
    op.execute("""
        UPDATE queues
        SET close_org_id = rotations.close_org_id
        FROM rotations
        WHERE queues.rotation_id = rotations.id
    """)


def downgrade():
    with op.batch_alter_table('queues', schema=None) as batch_op:
        batch_op.drop_index('ix_queues_close_org_id')
        batch_op.drop_column('close_org_id')

    with op.batch_alter_table('assignment_logs', schema=None) as batch_op:
        batch_op.drop_column('workflow_subscription_id')
