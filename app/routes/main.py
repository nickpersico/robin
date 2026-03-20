from flask import Blueprint, render_template
from flask_login import current_user

from ..extensions import db
from ..models.rotation import Rotation
from ..models.assignment_log import AssignmentLog
from ..models.queue import Queue

main_bp = Blueprint("main", __name__)


@main_bp.route("/")
def index():
    if not current_user.is_authenticated:
        return render_template("index.html")

    rotations = (
        Rotation.query
        .filter_by(close_org_id=current_user.close_org_id)
        .order_by(Rotation.created_at.desc())
        .all()
    )

    # Attach display helpers to each rotation
    for r in rotations:
        r.queue_count = r.queues.count()

        last_log = (
            AssignmentLog.query
            .join(Queue, AssignmentLog.queue_id == Queue.id)
            .filter(Queue.rotation_id == r.id)
            .order_by(AssignmentLog.assigned_at.desc())
            .first()
        )
        r.last_assignment_at = last_log.assigned_at if last_log else None

    return render_template("index.html", rotations=rotations)
