from datetime import datetime
from ..extensions import db
from ..utils import generate_id


class Organization(db.Model):
    """
    Represents a Close CRM organization that has signed up for Robin.

    The first user from a Close org to sign in creates the Organization record
    and becomes its admin. All subsequent users from the same org join as
    pending members until an admin approves them.
    """

    __tablename__ = "organizations"

    id = db.Column(db.String(20), primary_key=True, default=lambda: generate_id("og"))
    close_org_id = db.Column(db.String(64), unique=True, nullable=False, index=True)
    name = db.Column(db.String(255), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    users = db.relationship("User", back_populates="organization", lazy="dynamic")

    @property
    def pending_users(self):
        return self.users.filter_by(status="pending")

    @property
    def pending_count(self):
        return self.pending_users.count()

    def __repr__(self):
        return f"<Organization {self.close_org_id} ({self.name!r})>"
