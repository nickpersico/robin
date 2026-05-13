from datetime import datetime
from ..extensions import db
from ..utils import generate_id


class AssignmentLog(db.Model):
    """
    Records an event triggered by a Lead List for a single lead. Depending on
    which actions were enabled and which succeeded, a row may represent:
      - a lead assignment (rotation_member / close_user fields set)
      - a workflow trigger (workflow_id / workflow_name set)
      - both, when a list runs both actions

    Lead, user, and workflow info is denormalized so the UI never needs a Close
    API call to render history.
    """

    __tablename__ = "assignment_logs"

    id = db.Column(db.String(20), primary_key=True, default=lambda: generate_id("al"))
    # DB column / FK still names the column queue_id for back-compat.
    queue_id = db.Column(
        db.String(20), db.ForeignKey("queues.id"), nullable=False, index=True
    )
    rotation_member_id = db.Column(
        db.String(20), db.ForeignKey("rotation_members.id"), nullable=True, index=True
    )

    # Close Lead info (denormalized)
    close_lead_id = db.Column(db.String(64), nullable=False, index=True)
    close_lead_name = db.Column(db.String(255))

    # Close user info — null for workflow-only events
    close_user_id = db.Column(db.String(64), nullable=True)
    close_user_name = db.Column(db.String(255))

    # Workflow info — null for assignment-only events
    workflow_id = db.Column(db.String(64), nullable=True)
    workflow_name = db.Column(db.String(255), nullable=True)
    # The Close sequence_subscription id returned by the API — lets the UI
    # deep-link the history row to the workflow run.
    workflow_subscription_id = db.Column(db.String(64), nullable=True)

    assigned_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    lead_list = db.relationship("LeadList", back_populates="assignment_logs")
    rotation_member = db.relationship("RotationMember", back_populates="assignment_logs")

    @property
    def was_assigned(self):
        return self.close_user_id is not None

    @property
    def was_workflow_triggered(self):
        return self.workflow_id is not None

    def __repr__(self):
        return (
            f"<AssignmentLog lead={self.close_lead_id} user={self.close_user_id} "
            f"workflow={self.workflow_id} at={self.assigned_at}>"
        )
