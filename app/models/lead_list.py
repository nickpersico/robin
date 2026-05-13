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


class LeadList(db.Model):
    """
    A top-level object owned by an organization. Each Lead List defines:
      - which leads qualify (filters_json)
      - which Actions Robin should run when a matching lead appears:
          * Assign a Lead — uses a Group (Rotation) and writes the assignee
            into a Close custom field.
          * Trigger a Workflow — triggers a Close Workflow (Sequence) on
            the lead's first contact. Only Workflows that can be manually
            triggered on Leads are eligible.
        Either or both actions can be enabled.

    The database table is still named ``queues`` for back-compat; the Python
    class is ``LeadList`` and the user-facing term is "Lead List" everywhere.
    """

    __tablename__ = "queues"

    id = db.Column(db.String(20), primary_key=True, default=lambda: generate_id("qu"))

    # Denormalised so workflow-only Lead Lists (no rotation) are still org-scoped.
    # Nullable for backwards compatibility — pre-migration rows are backfilled from
    # the linked rotation; all new rows must set this explicitly.
    close_org_id = db.Column(db.String(64), nullable=True, index=True)

    # Nullable: only required when assign_enabled is True.
    rotation_id = db.Column(
        db.String(20), db.ForeignKey("rotations.id"), nullable=True, index=True
    )

    name = db.Column(db.String(255), nullable=False)
    queue_type = db.Column(db.String(32), nullable=False, default=TYPE_ONGOING)

    # Conditions: which leads to process
    filters_json = db.Column(db.JSON, nullable=True)

    # ── Action 1: Assign a Lead ─────────────────────────────────────────────
    assign_enabled = db.Column(db.Boolean, default=True, nullable=False)
    custom_field_id = db.Column(db.String(64), nullable=True)
    custom_field_label = db.Column(db.String(255), nullable=True)  # denormalized
    overwrite_existing = db.Column(db.Boolean, default=False, nullable=False)

    # ── Action 2: Trigger a Workflow ────────────────────────────────────────
    workflow_enabled = db.Column(db.Boolean, default=False, nullable=False)
    workflow_id = db.Column(db.String(64), nullable=True)
    workflow_name = db.Column(db.String(255), nullable=True)  # denormalized
    # The Close user to "run" the workflow as: their email account sends
    # emails, their phone sends SMS, call/task steps assign to them.
    # NULL means "use the rotation member assigned to this lead" — only valid
    # when assign_enabled is True. Required when workflow_enabled and
    # assign_enabled is False.
    workflow_run_as_user_id = db.Column(db.String(64), nullable=True)
    workflow_run_as_user_name = db.Column(db.String(255), nullable=True)  # denormalized

    # Operational state
    status = db.Column(db.String(32), nullable=False, default=STATUS_PENDING)
    last_checked_at = db.Column(db.DateTime, nullable=True)
    # Lead IDs that existed at creation — never act on these
    seeded_lead_ids = db.Column(db.JSON, nullable=True, default=list)

    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    updated_at = db.Column(
        db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, nullable=False
    )

    rotation = db.relationship("Rotation", back_populates="lead_lists")
    assignment_logs = db.relationship(
        "AssignmentLog",
        back_populates="lead_list",
        cascade="all, delete-orphan",
        lazy="dynamic",
    )

    # ── Helpers ─────────────────────────────────────────────────────────────

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
        if not self.filters_json:
            return False
        if self.assign_enabled and not (self.rotation_id and self.custom_field_id):
            return False
        if self.workflow_enabled:
            if not self.workflow_id:
                return False
            # Must have an explicit run-as user OR inherit from the assignee.
            if not self.workflow_run_as_user_id and not self.assign_enabled:
                return False
        return self.assign_enabled or self.workflow_enabled

    @property
    def action_summary(self):
        """Short label for list views, e.g. 'Assign + Workflow' / 'Workflow only'."""
        parts = []
        if self.assign_enabled:
            parts.append("Assign")
        if self.workflow_enabled:
            parts.append("Workflow")
        return " + ".join(parts) if parts else "—"

    def __repr__(self):
        return f"<LeadList {self.id} '{self.name}'>"
