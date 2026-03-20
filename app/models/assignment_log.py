from datetime import datetime
from ..extensions import db
from ..utils import generate_id


class AssignmentLog(db.Model):
    """
    A record of a Lead being assigned to a RotationMember via a Queue.
    Lead and user info is denormalized so it's readable without API calls.
    """

    __tablename__ = "assignment_logs"

    id = db.Column(db.String(20), primary_key=True, default=lambda: generate_id("al"))
    queue_id = db.Column(
        db.String(20), db.ForeignKey("queues.id"), nullable=False, index=True
    )
    rotation_member_id = db.Column(
        db.String(20), db.ForeignKey("rotation_members.id"), nullable=True, index=True
    )

    # Close Lead info (denormalized)
    close_lead_id = db.Column(db.String(64), nullable=False, index=True)
    close_lead_name = db.Column(db.String(255))

    # Close user info at time of assignment
    close_user_id = db.Column(db.String(64), nullable=False)
    close_user_name = db.Column(db.String(255))

    assigned_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    queue = db.relationship("Queue", back_populates="assignment_logs")
    rotation_member = db.relationship("RotationMember", back_populates="assignment_logs")

    def __repr__(self):
        return (
            f"<AssignmentLog lead={self.close_lead_id} user={self.close_user_id} "
            f"at={self.assigned_at}>"
        )
