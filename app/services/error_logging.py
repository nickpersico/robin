"""
Persist user-facing errors (500s, and 404s for signed-in users) to the
error_logs table so support can triage them from /system.

Recording is strictly best-effort: it must never raise, since it runs from
inside error handlers. If the DB write fails (e.g. the original error was a DB
outage), we swallow it — the traceback is already going to the app logger.
"""
import traceback as _traceback
from typing import Optional

from flask import request, current_app
from flask_login import current_user

from ..extensions import db
from ..models.error_log import ErrorLog


def _truncate(value, limit):
    if value is None:
        return None
    value = str(value)
    return value if len(value) <= limit else value[: limit - 1] + "…"


def log_error(status_code: int, exc: Optional[Exception] = None) -> None:
    """
    Write one ErrorLog row for the current request. Safe to call from any
    error handler; never raises.
    """
    try:
        tb = None
        error_type = None
        error_message = None
        if exc is not None:
            error_type = _truncate(exc.__class__.__name__, 128)
            error_message = _truncate(exc, 2000)
            tb = "".join(
                _traceback.format_exception(type(exc), exc, exc.__traceback__)
            )
        elif status_code == 404:
            error_type = "NotFound"

        # Identify the user if authenticated (current_user is an anonymous
        # proxy otherwise). Guard every attribute — the proxy can be in odd
        # states inside an error handler.
        user_id = None
        user_email = None
        close_org_id = None
        try:
            if getattr(current_user, "is_authenticated", False):
                user_id = current_user.get_id()
                user_email = _truncate(getattr(current_user, "email", None), 255)
                close_org_id = _truncate(getattr(current_user, "close_org_id", None), 64)
        except Exception:  # pragma: no cover - defensive
            pass

        entry = ErrorLog(
            status_code=status_code,
            error_type=error_type,
            error_message=error_message,
            traceback=tb,
            method=_truncate(request.method, 8),
            path=_truncate(request.path, 512),  # path only — never the query string
            endpoint=_truncate(request.endpoint, 128),
            referrer=_truncate(request.referrer, 512),
            user_id=user_id,
            user_email=user_email,
            close_org_id=close_org_id,
            user_agent=_truncate(request.headers.get("User-Agent"), 512),
            remote_addr=_truncate(request.remote_addr, 64),
        )
        db.session.add(entry)
        db.session.commit()
    except Exception:
        # Logging the error must not itself take down the request.
        try:
            db.session.rollback()
            current_app.logger.exception("Failed to persist ErrorLog")
        except Exception:  # pragma: no cover - defensive
            pass
