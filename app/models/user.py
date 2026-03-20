from datetime import datetime
from flask_login import UserMixin
from ..extensions import db, login_manager
from ..utils import generate_id

ROLE_ADMIN = "admin"
ROLE_MEMBER = "member"

STATUS_ACTIVE = "active"
STATUS_PENDING = "pending"
STATUS_SUSPENDED = "suspended"


class User(UserMixin, db.Model):
    """
    An app user authenticated via Close OAuth.
    Identified by their Close user_id (e.g. "user_abc123").

    role:   'admin'  — can manage the org's rotations, queues, and users
            'member' — read-only view of their own assignment stats
    status: 'active'    — full access
            'pending'   — signed in but not yet approved by an admin
            'suspended' — Close membership deactivated; access revoked
    """

    __tablename__ = "users"

    __table_args__ = (
        db.UniqueConstraint("close_user_id", "close_org_id", name="uq_users_close_user_org"),
    )

    id = db.Column(db.String(20), primary_key=True, default=lambda: generate_id("us"))
    close_user_id = db.Column(db.String(64), nullable=False, index=True)
    close_org_id = db.Column(db.String(64), nullable=False, index=True)

    # Org relationship (nullable to allow backfill for pre-existing users)
    organization_id = db.Column(
        db.String(20), db.ForeignKey("organizations.id"), nullable=True, index=True
    )

    role = db.Column(db.String(32), nullable=False, default=ROLE_MEMBER, server_default=ROLE_MEMBER)
    status = db.Column(db.String(32), nullable=False, default=STATUS_ACTIVE, server_default=STATUS_ACTIVE)

    email = db.Column(db.String(255), nullable=False)
    first_name = db.Column(db.String(128))
    last_name = db.Column(db.String(128))

    access_token = db.Column(db.Text, nullable=False)
    refresh_token = db.Column(db.Text)
    token_expires_at = db.Column(db.DateTime)

    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    last_login_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    organization = db.relationship("Organization", back_populates="users")
    rotations = db.relationship("Rotation", back_populates="owner", lazy="dynamic")

    # ------------------------------------------------------------------
    # Convenience properties
    # ------------------------------------------------------------------

    @property
    def full_name(self):
        parts = [self.first_name, self.last_name]
        return " ".join(p for p in parts if p) or self.email

    @property
    def is_admin(self):
        return self.role == ROLE_ADMIN

    @property
    def is_active_user(self):
        """True when the user has full access (not pending or suspended)."""
        return self.status == STATUS_ACTIVE

    @property
    def is_pending(self):
        return self.status == STATUS_PENDING

    @property
    def is_suspended(self):
        return self.status == STATUS_SUSPENDED

    @property
    def org_name(self):
        """Convenience: org name or fallback to close_org_id."""
        if self.organization:
            return self.organization.name
        return self.close_org_id

    def __repr__(self):
        return f"<User {self.close_user_id} ({self.email})>"


@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, user_id)
