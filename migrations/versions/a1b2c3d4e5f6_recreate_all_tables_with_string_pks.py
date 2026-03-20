"""Recreate all tables with string PKs

Revision ID: a1b2c3d4e5f6
Revises: cae22ca131e3
Create Date: 2026-03-20 00:00:00.000000

The original migrations used auto-increment integer PKs. All models were
updated to use prefixed string PKs (og_, us_, ro_, rm_, qu_, al_). This
migration drops the old tables and recreates them with the correct schema.
"""

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = 'a1b2c3d4e5f6'
down_revision = '3d4a636f309f'
branch_labels = None
depends_on = None


def upgrade():
    # Drop all old tables in dependency order (children first)
    op.execute("DROP TABLE IF EXISTS assignment_logs CASCADE")
    op.execute("DROP TABLE IF EXISTS queues CASCADE")
    op.execute("DROP TABLE IF EXISTS rotation_members CASCADE")
    op.execute("DROP TABLE IF EXISTS rotations CASCADE")
    op.execute("DROP TABLE IF EXISTS users CASCADE")
    op.execute("DROP TABLE IF EXISTS organizations CASCADE")

    # ── organizations ─────────────────────────────────────────────────────
    op.create_table(
        'organizations',
        sa.Column('id',           sa.String(20),  primary_key=True),
        sa.Column('close_org_id', sa.String(64),  nullable=False, unique=True),
        sa.Column('name',         sa.String(255), nullable=False),
        sa.Column('created_at',   sa.DateTime(),  nullable=False),
    )
    op.create_index('ix_organizations_close_org_id', 'organizations', ['close_org_id'])

    # ── users ─────────────────────────────────────────────────────────────
    op.create_table(
        'users',
        sa.Column('id',              sa.String(20),  primary_key=True),
        sa.Column('close_user_id',   sa.String(64),  nullable=False),
        sa.Column('close_org_id',    sa.String(64),  nullable=False),
        sa.Column('organization_id', sa.String(20),  sa.ForeignKey('organizations.id'), nullable=True),
        sa.Column('role',            sa.String(32),  nullable=False, server_default='member'),
        sa.Column('status',          sa.String(32),  nullable=False, server_default='active'),
        sa.Column('email',           sa.String(255), nullable=False),
        sa.Column('first_name',      sa.String(128), nullable=True),
        sa.Column('last_name',       sa.String(128), nullable=True),
        sa.Column('access_token',    sa.Text(),      nullable=False),
        sa.Column('refresh_token',   sa.Text(),      nullable=True),
        sa.Column('token_expires_at',sa.DateTime(),  nullable=True),
        sa.Column('created_at',      sa.DateTime(),  nullable=False),
        sa.Column('last_login_at',   sa.DateTime(),  nullable=False),
        sa.UniqueConstraint('close_user_id', 'close_org_id', name='uq_users_close_user_org'),
    )
    op.create_index('ix_users_close_user_id',   'users', ['close_user_id'])
    op.create_index('ix_users_close_org_id',    'users', ['close_org_id'])
    op.create_index('ix_users_organization_id', 'users', ['organization_id'])

    # ── rotations ─────────────────────────────────────────────────────────
    op.create_table(
        'rotations',
        sa.Column('id',            sa.String(20),  primary_key=True),
        sa.Column('name',          sa.String(255), nullable=False),
        sa.Column('description',   sa.Text(),      nullable=True),
        sa.Column('owner_id',      sa.String(20),  sa.ForeignKey('users.id'), nullable=False),
        sa.Column('close_org_id',  sa.String(64),  nullable=False),
        sa.Column('current_index', sa.Integer(),   nullable=False, server_default='0'),
        sa.Column('created_at',    sa.DateTime(),  nullable=False),
        sa.Column('updated_at',    sa.DateTime(),  nullable=False),
    )
    op.create_index('ix_rotations_close_org_id', 'rotations', ['close_org_id'])

    # ── rotation_members ──────────────────────────────────────────────────
    op.create_table(
        'rotation_members',
        sa.Column('id',              sa.String(20),  primary_key=True),
        sa.Column('rotation_id',     sa.String(20),  sa.ForeignKey('rotations.id'), nullable=False),
        sa.Column('close_user_id',   sa.String(64),  nullable=False),
        sa.Column('close_user_email',sa.String(255), nullable=True),
        sa.Column('close_user_name', sa.String(255), nullable=True),
        sa.Column('position',        sa.Integer(),   nullable=False, server_default='0'),
        sa.Column('is_active',       sa.Boolean(),   nullable=False, server_default='true'),
        sa.Column('added_at',        sa.DateTime(),  nullable=False),
        sa.UniqueConstraint('rotation_id', 'close_user_id', name='uq_rotation_members_rotation_user'),
    )
    op.create_index('ix_rotation_members_rotation_id', 'rotation_members', ['rotation_id'])

    # ── queues ────────────────────────────────────────────────────────────
    op.create_table(
        'queues',
        sa.Column('id',                 sa.String(20),  primary_key=True),
        sa.Column('rotation_id',        sa.String(20),  sa.ForeignKey('rotations.id'), nullable=False),
        sa.Column('name',               sa.String(255), nullable=False),
        sa.Column('queue_type',         sa.String(32),  nullable=False, server_default='one_time'),
        sa.Column('filters_json',       sa.JSON(),      nullable=True),
        sa.Column('custom_field_id',    sa.String(64),  nullable=True),
        sa.Column('custom_field_label', sa.String(255), nullable=True),
        sa.Column('overwrite_existing', sa.Boolean(),   nullable=False, server_default='false'),
        sa.Column('status',             sa.String(32),  nullable=False, server_default='pending'),
        sa.Column('last_checked_at',    sa.DateTime(),  nullable=True),
        sa.Column('seeded_lead_ids',    sa.JSON(),      nullable=True),
        sa.Column('created_at',         sa.DateTime(),  nullable=False),
        sa.Column('updated_at',         sa.DateTime(),  nullable=False),
    )
    op.create_index('ix_queues_rotation_id', 'queues', ['rotation_id'])

    # ── assignment_logs ───────────────────────────────────────────────────
    op.create_table(
        'assignment_logs',
        sa.Column('id',                 sa.String(20),  primary_key=True),
        sa.Column('queue_id',           sa.String(20),  sa.ForeignKey('queues.id'), nullable=False),
        sa.Column('rotation_member_id', sa.String(20),  sa.ForeignKey('rotation_members.id'), nullable=False),
        sa.Column('close_lead_id',      sa.String(64),  nullable=False),
        sa.Column('close_lead_name',    sa.String(255), nullable=True),
        sa.Column('close_user_id',      sa.String(64),  nullable=False),
        sa.Column('close_user_name',    sa.String(255), nullable=True),
        sa.Column('assigned_at',        sa.DateTime(),  nullable=False),
    )
    op.create_index('ix_assignment_logs_queue_id',           'assignment_logs', ['queue_id'])
    op.create_index('ix_assignment_logs_rotation_member_id', 'assignment_logs', ['rotation_member_id'])
    op.create_index('ix_assignment_logs_close_lead_id',      'assignment_logs', ['close_lead_id'])


def downgrade():
    op.execute("DROP TABLE IF EXISTS assignment_logs CASCADE")
    op.execute("DROP TABLE IF EXISTS queues CASCADE")
    op.execute("DROP TABLE IF EXISTS rotation_members CASCADE")
    op.execute("DROP TABLE IF EXISTS rotations CASCADE")
    op.execute("DROP TABLE IF EXISTS users CASCADE")
    op.execute("DROP TABLE IF EXISTS organizations CASCADE")
