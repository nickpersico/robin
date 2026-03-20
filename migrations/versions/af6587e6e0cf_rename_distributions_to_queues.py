"""rename distributions to queues

Revision ID: af6587e6e0cf
Revises: cae22ca131e3
Create Date: 2026-03-15 20:47:59.100617

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'af6587e6e0cf'
down_revision = 'cae22ca131e3'
branch_labels = None
depends_on = None


def upgrade():
    # Rename the distributions table to queues
    op.rename_table('distributions', 'queues')

    # Rename the distribution_type column to queue_type on the queues table
    op.alter_column('queues', 'distribution_type', new_column_name='queue_type')

    # Rename the index on queues.rotation_id
    op.execute('ALTER INDEX ix_distributions_rotation_id RENAME TO ix_queues_rotation_id')

    # Rename distribution_id to queue_id on assignment_logs
    op.alter_column('assignment_logs', 'distribution_id', new_column_name='queue_id')

    # Rename the index on assignment_logs.queue_id
    op.execute('ALTER INDEX ix_assignment_logs_distribution_id RENAME TO ix_assignment_logs_queue_id')


def downgrade():
    # Reverse: rename queues back to distributions
    op.execute('ALTER INDEX ix_assignment_logs_queue_id RENAME TO ix_assignment_logs_distribution_id')

    op.alter_column('assignment_logs', 'queue_id', new_column_name='distribution_id')

    op.execute('ALTER INDEX ix_queues_rotation_id RENAME TO ix_distributions_rotation_id')

    op.alter_column('queues', 'queue_type', new_column_name='distribution_type')

    op.rename_table('queues', 'distributions')
