from datetime import datetime
from ..extensions import db
from ..utils import generate_id


class ErrorLog(db.Model):
    """
    Records an error page shown to a user — a 500 (unhandled exception) or a
    404 hit by a signed-in user. Exists so support can triage "it broke for
    customer X" from the /system dashboard without SSHing in to read logs.

    Everything is denormalized (user email, org id) so the dashboard never
    needs a join or a Close API call to render history, and rows survive even
    if the user record is later deleted.
    """

    __tablename__ = "error_logs"

    id = db.Column(db.String(20), primary_key=True, default=lambda: generate_id("er"))
    occurred_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False, index=True)

    # 404 or 500 (room for other codes later).
    status_code = db.Column(db.Integer, nullable=False, index=True)
    # Exception class name for 500s (e.g. "OperationalError"); "NotFound" for 404s.
    error_type = db.Column(db.String(128))
    # str(exception), truncated. Null for a plain 404.
    error_message = db.Column(db.Text)
    # Full traceback for 500s; null for 404s.
    traceback = db.Column(db.Text)

    # What they were doing — request context.
    method = db.Column(db.String(8))
    # request.path only (no query string — it can carry OAuth codes/tokens).
    path = db.Column(db.String(512), index=True)
    # Flask endpoint, e.g. "auth.callback" — the human-readable "what they tried".
    endpoint = db.Column(db.String(128))
    referrer = db.Column(db.String(512))

    # Who — all nullable; anonymous visitors hit errors too (e.g. during login).
    user_id = db.Column(db.String(20), db.ForeignKey("users.id"), nullable=True, index=True)
    user_email = db.Column(db.String(255), index=True)
    close_org_id = db.Column(db.String(64), index=True)

    user_agent = db.Column(db.String(512))
    remote_addr = db.Column(db.String(64))

    user = db.relationship("User")

    @property
    def is_server_error(self):
        return self.status_code >= 500

    @property
    def actor(self):
        """Display label for who hit the error."""
        return self.user_email or "(anonymous)"

    def __repr__(self):
        return (
            f"<ErrorLog {self.status_code} {self.method} {self.path} "
            f"user={self.user_email} at={self.occurred_at}>"
        )
