from datetime import datetime
from ..extensions import db
from ..utils import generate_id


class Rotation(db.Model):
    """
    A named, ordered list of Close users who receive leads in turn.
    The round-robin pointer (current_index) lives here and is shared
    across all Queues that use this Rotation.
    """

    __tablename__ = "rotations"

    id = db.Column(db.String(20), primary_key=True, default=lambda: generate_id("ro"))
    name = db.Column(db.String(255), nullable=False)
    description = db.Column(db.Text)

    owner_id = db.Column(db.String(20), db.ForeignKey("users.id"), nullable=False)
    close_org_id = db.Column(db.String(64), nullable=False, index=True)

    # Shared round-robin pointer across all Queues using this Rotation
    current_index = db.Column(db.Integer, default=0, nullable=False)

    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(
        db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )

    owner = db.relationship("User", back_populates="rotations")
    members = db.relationship(
        "RotationMember",
        back_populates="rotation",
        order_by="RotationMember.position",
        cascade="all, delete-orphan",
    )
    queues = db.relationship(
        "Queue",
        back_populates="rotation",
        cascade="all, delete-orphan",
        lazy="dynamic",
    )

    def next_member(self):
        """Return the RotationMember who should receive the next lead."""
        active_members = [m for m in self.members if m.is_active]
        if not active_members:
            return None
        return active_members[self.current_index % len(active_members)]

    def advance(self):
        """Advance the pointer to the next member."""
        active_members = [m for m in self.members if m.is_active]
        if active_members:
            self.current_index = (self.current_index + 1) % len(active_members)

    def __repr__(self):
        return f"<Rotation {self.id} '{self.name}'>"


class RotationMember(db.Model):
    """
    A Close user who is part of a Rotation.
    Name and email are stored denormalized to avoid API calls for display.
    """

    __tablename__ = "rotation_members"

    id = db.Column(db.String(20), primary_key=True, default=lambda: generate_id("rm"))
    rotation_id = db.Column(
        db.String(20), db.ForeignKey("rotations.id"), nullable=False, index=True
    )

    close_user_id = db.Column(db.String(64), nullable=False)
    close_user_email = db.Column(db.String(255))
    close_user_name = db.Column(db.String(255))

    position = db.Column(db.Integer, nullable=False, default=0)
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    added_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    rotation = db.relationship("Rotation", back_populates="members")
    assignment_logs = db.relationship(
        "AssignmentLog",
        back_populates="rotation_member",
        lazy="dynamic",
        passive_deletes=True,
    )

    __table_args__ = (
        db.UniqueConstraint("rotation_id", "close_user_id", name="uq_rotation_user"),
    )

    @property
    def assignment_count(self):
        return self.assignment_logs.count()

    def __repr__(self):
        return f"<RotationMember {self.close_user_name} in rotation {self.rotation_id}>"
