import logging
import os

from flask import Flask, redirect, url_for, request, flash
from flask_login import current_user, logout_user
from .config import Config
from .extensions import db, migrate, login_manager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)


def create_app(config_class=Config):
    app = Flask(__name__)
    app.config.from_object(config_class)

    db.init_app(app)
    migrate.init_app(app, db)
    login_manager.init_app(app)

    from .models import user  # noqa: F401 - needed for Flask-Login user_loader

    from .routes.main import main_bp
    from .routes.auth import auth_bp
    from .routes.rotations import rotations_bp
    from .routes.queues import queues_bp
    from .routes.admin import admin_bp
    from .routes.activity import activity_bp
    from .routes.help import help_bp
    from .routes.legal import legal_bp

    app.register_blueprint(main_bp)
    app.register_blueprint(auth_bp, url_prefix="/auth")
    app.register_blueprint(rotations_bp, url_prefix="/groups")
    app.register_blueprint(queues_bp)
    app.register_blueprint(admin_bp)
    app.register_blueprint(activity_bp)
    app.register_blueprint(help_bp)
    app.register_blueprint(legal_bp)

    # ── Template context ─────────────────────────────────────────────────────
    from .models.user import User as _User

    @app.context_processor
    def inject_user_orgs():
        if current_user.is_authenticated and current_user.email:
            orgs = (
                _User.query
                .filter_by(email=current_user.email)
                .filter(_User.status != "suspended")
                .order_by(_User.created_at)
                .all()
            )
            return {"user_orgs": orgs}
        return {"user_orgs": []}

    # ── Status gate ──────────────────────────────────────────────────────────
    # Pending users have read-only access: they can view all pages but cannot
    # create, edit, or delete anything until approved by an admin.
    # Suspended users are logged out immediately.
    _PENDING_BLOCKED_ENDPOINTS = {
        "rotations.create_rotation",
        "rotations.edit_rotation",
        "rotations.delete_rotation",
        "queues.create_queue",
        "queues.edit_queue",
        "queues.delete_queue",
        "queues.toggle_queue",
        "queues.check_queue",
    }

    @app.before_request
    def check_user_status():
        if not current_user.is_authenticated:
            return
        endpoint = getattr(request, "endpoint", None)
        if current_user.is_suspended:
            logout_user()
            flash("Your Robin access has been suspended.", "error")
            return redirect(url_for("main.index"))
        if current_user.is_pending and endpoint in _PENDING_BLOCKED_ENDPOINTS:
            flash("Your account is pending approval — you have read-only access until an admin approves you.", "warning")
            return redirect(url_for("main.index"))

    # ── Scheduler ────────────────────────────────────────────────────────────
    # Start APScheduler only in the real worker process, not in Flask's
    # reloader parent process (which would run the job twice per interval).
    if not app.debug or os.environ.get("WERKZEUG_RUN_MAIN") == "true":
        from apscheduler.schedulers.background import BackgroundScheduler
        from .services.assignment_engine import poll_all_queues

        scheduler = BackgroundScheduler(timezone="UTC")

        def _poll_job():
            with app.app_context():
                poll_all_queues()

        scheduler.add_job(
            func=_poll_job,
            trigger="interval",
            minutes=5,
            id="poll_queues",
            replace_existing=True,
        )
        scheduler.start()
        app.logger.info("Scheduler started — polling every 5 minutes.")

    # ── CLI commands ─────────────────────────────────────────────────────────
    import click

    @app.cli.command("make-admin")
    @click.argument("email")
    def make_admin(email):
        """Promote a user to admin by their email address."""
        from .models.user import User, ROLE_ADMIN, STATUS_ACTIVE
        from .models.organization import Organization

        user = User.query.filter_by(email=email).first()
        if user is None:
            click.echo(f"No user found with email: {email}", err=True)
            raise SystemExit(1)

        # Ensure org record exists; create a minimal one if not
        if user.organization_id is None:
            org = Organization.query.filter_by(close_org_id=user.close_org_id).first()
            if org is None:
                org = Organization(close_org_id=user.close_org_id, name=user.close_org_id)
                db.session.add(org)
                db.session.flush()
            user.organization_id = org.id

        user.role = ROLE_ADMIN
        user.status = STATUS_ACTIVE
        db.session.commit()
        click.echo(f"✓ {user.full_name} ({email}) is now an admin.")

    return app
