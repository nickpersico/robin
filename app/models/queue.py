from datetime import datetime
from ..extensions import db
from ..utils import generate_id

# queue_type values
TYPE_ONE_TIME = "one_time"
TYPE_ONGOING = "ongoing"
TYPE_BACKFILL_AND_WATCH = "backfill_and_watch"

QUEUE_TYPES = [
    (TYPE_ONE_TIME, "One-time"),
    (TYPE_ONGOING, "Ongoing"),
    (TYPE_BACKFILL_AND_WATCH, "Backfill & Watch"),
]

# status values
STATUS_PENDING = "pending"      # created, not yet run (one_time / backfill_and_watch pre-backfill)
STATUS_RUNNING = "running"      # backfill currently in progress
STATUS_ACTIVE = "active"        # ongoing / watching for new leads
STATUS_PAUSED = "paused"        # manually paused
STATUS_COMPLETED = "completed"  # one_time finished
STATUS_FAILED = "failed"        # error during run


class Queue(db.Model):
    """
    The operational unit that connects a Rotation to a set of lead conditions.

    A Queue defines:
    - Which leads qualify (filters_json)
    - Which Close custom field to write the assignment into
    - Whether to overwrite existing values
    - How to distribute (one_time, ongoing, or backfill_and_watch)

    Multiple Queues can share one Rotation; they share its round-robin
    pointer so leads are distributed evenly across all sources.
    """

    __tablename__ = "queues"

    id = db.Column(db.String(20), primary_key=True, default=lambda: generate_id("qu"))
    rotation_id = db.Column(
        db.String(20), db.ForeignKey("rotations.id"), nullable=False, index=True
    )

    name = db.Column(db.String(255), nullable=False)
    queue_type = db.Column(db.String(32), nullable=False, default=TYPE_ONE_TIME)

    # Conditions: which leads to assign
    filters_json = db.Column(db.JSON, nullable=True)

    # Assignment target
    custom_field_id = db.Column(db.String(64), nullable=True)
    custom_field_label = db.Column(db.String(255), nullable=True)  # denormalized
    overwrite_existing = db.Column(db.Boolean, default=False, nullable=False)

    # Operational state
    status = db.Column(db.String(32), nullable=False, default=STATUS_PENDING)
    # For ongoing/backfill_and_watch: timestamp of the last successful poll
    last_checked_at = db.Column(db.DateTime, nullable=True)
    # Lead IDs that existed in the filter at queue creation — never assign these
    seeded_lead_ids = db.Column(db.JSON, nullable=True, default=list)

    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(
        db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )

    rotation = db.relationship("Rotation", back_populates="queues")
    assignment_logs = db.relationship(
        "AssignmentLog", back_populates="queue", lazy="dynamic"
    )

    @property
    def is_active(self):
        return self.status in (STATUS_ACTIVE, STATUS_RUNNING)

    @property
    def type_label(self):
        return {
            TYPE_ONE_TIME: "One-time",
            TYPE_ONGOING: "Ongoing",
            TYPE_BACKFILL_AND_WATCH: "Backfill & Watch",
        }.get(self.queue_type, self.queue_type)

    @property
    def is_configured(self):
        return bool(self.filters_json and self.custom_field_id)

    def __repr__(self):
        return f"<Queue {self.id} '{self.name}' ({self.queue_type})>"
