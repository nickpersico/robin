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
    from .routes.lead_lists import lead_lists_bp
    from .routes.queues import queues_bp
    from .routes.admin import admin_bp
    from .routes.activity import activity_bp
    from .routes.help import help_bp
    from .routes.legal import legal_bp

    app.register_blueprint(main_bp)
    app.register_blueprint(auth_bp, url_prefix="/auth")
    app.register_blueprint(rotations_bp, url_prefix="/groups")
    app.register_blueprint(lead_lists_bp)
    app.register_blueprint(queues_bp)  # legacy URL redirects only
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
        "lead_lists.create_lead_list",
        "lead_lists.edit_lead_list",
        "lead_lists.delete_lead_list",
        "lead_lists.toggle_lead_list",
        "lead_lists.check_lead_list",
        # Legacy endpoints — still register them so users on stale tabs
        # get blocked at the redirect step instead of after.
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

    @app.cli.command("inspect-org")
    @click.option("--email", help="Look up every org containing a user with this email.")
    @click.option("--org-id", help="Look up a single org by Close org id (orga_...).")
    def inspect_org(email, org_id):
        """
        Dump a Close org's Robin configuration for support triage.

        --email finds every Robin User row with that address (a person can
        be in multiple Close orgs, one User row per org) and dumps each
        org's config in turn. --org-id looks up one org directly.

        Prints per org:
          - the matched user (with --email) + role/status
          - every Group with its members (position, active flag, name, email)
          - every Lead List with status, actions, target field, rotation
            link, overwrite, and workflow run-as
        """
        from sqlalchemy import func as _func
        from .models.user import User
        from .models.rotation import Rotation
        from .models.lead_list import LeadList

        if not email and not org_id:
            click.echo("Provide either --email or --org-id.", err=True)
            raise SystemExit(2)

        def _dump_org(org, user=None):
            if user is not None:
                full_name = (
                    f"{user.first_name or ''} {user.last_name or ''}".strip()
                    or "(no name)"
                )
                click.echo(
                    f"User:  {user.email}  ({full_name})  "
                    f"role={user.role}  status={user.status}"
                )
            click.echo(f"Org:   {org}")

            rotations = (
                Rotation.query
                .filter_by(close_org_id=org)
                .order_by(Rotation.created_at)
                .all()
            )
            click.echo(f"\nGroups: {len(rotations)}")
            for r in rotations:
                click.echo(
                    f"  {r.id}  {r.name!r}  current_index={r.current_index}  "
                    f"members={len(r.members)}"
                )
                for m in sorted(r.members, key=lambda x: x.position):
                    click.echo(
                        f"    [{m.position}] active={m.is_active}  {m.close_user_id}  "
                        f"{m.close_user_name!r}  <{m.close_user_email}>"
                    )

            lead_lists = (
                LeadList.query
                .filter_by(close_org_id=org)
                .order_by(LeadList.created_at)
                .all()
            )
            click.echo(f"\nLead Lists: {len(lead_lists)}")
            for ll in lead_lists:
                actions = []
                if ll.assign_enabled:
                    actions.append("assign")
                if ll.workflow_enabled:
                    actions.append("workflow")
                click.echo(
                    f"  {ll.id}  {ll.name!r}  status={ll.status}  "
                    f"actions={'+'.join(actions) or 'none'}"
                )
                if ll.assign_enabled:
                    click.echo(
                        f"    rotation={ll.rotation_id}  field={ll.custom_field_label!r}  "
                        f"overwrite={ll.overwrite_existing}"
                    )
                if ll.workflow_enabled:
                    click.echo(
                        f"    workflow={ll.workflow_name!r} ({ll.workflow_id})  "
                        f"run_as={ll.workflow_run_as_user_name or 'assigned member'}"
                    )

        if email:
            users = (
                User.query
                .filter(_func.lower(User.email) == email.lower())
                .order_by(User.created_at)
                .all()
            )
            if not users:
                click.echo(f"No user found with email: {email}", err=True)
                raise SystemExit(1)
            click.echo(
                f"Found {len(users)} Robin user record(s) for {email}"
                f" across {len({u.close_org_id for u in users})} org(s).\n"
            )
            for i, user in enumerate(users):
                if i > 0:
                    click.echo("\n" + "=" * 60 + "\n")
                _dump_org(user.close_org_id, user=user)
        else:
            _dump_org(org_id)

    @app.cli.command("check-backlog")
    @click.option("--org", help="Only inspect Lead Lists in this Close org id.")
    def check_backlog(org):
        """
        Report how many leads each active Lead List would act on if polled
        right now — without touching anything.

        Runs the same Close search poll_queue would run, then reports:
          - matches:     unseeded leads created since last_checked_at
          - would_skip:  matches whose target custom field is already set
                         (only counted when overwrite_existing is False,
                          since those get skipped by the engine)
          - actionable:  matches - would_skip

        Use this after an outage to decide which lists actually need
        reseeding — a list with actionable=0 is safe to leave alone.
        """
        from .models.lead_list import LeadList, STATUS_ACTIVE
        from .services.close_api import CloseClient, CloseAPIError
        from .services.assignment_engine import (
            _normalize_filter,
            _inject_date_filter,
            _get_org_user,
        )

        q = LeadList.query.filter_by(status=STATUS_ACTIVE)
        if org:
            q = q.filter(LeadList.close_org_id == org)
        lists = q.order_by(LeadList.close_org_id, LeadList.name).all()

        if not lists:
            click.echo("No active Lead Lists found.")
            return

        click.echo(f"Checking {len(lists)} active Lead List(s)...\n")

        total_actionable = 0
        lists_with_backlog = 0

        for ll in lists:
            try:
                org_user = _get_org_user(ll.close_org_id)
                if not org_user:
                    click.echo(f"! {ll.id} {ll.name!r} — no active user for org, skipped")
                    continue

                client = CloseClient(org_user)
                after_dt = ll.last_checked_at or ll.created_at
                search_query = _inject_date_filter(
                    _normalize_filter(ll.filters_json), after_dt
                )
                leads = client.search_leads(search_query)

                seeded = set(ll.seeded_lead_ids or [])
                unseeded = [l for l in leads if l.get("id") not in seeded]

                would_skip = 0
                if (
                    ll.assign_enabled
                    and ll.custom_field_id
                    and not ll.overwrite_existing
                ):
                    would_skip = sum(
                        1 for l in unseeded
                        if (l.get("custom") or {}).get(ll.custom_field_id)
                    )

                matches = len(unseeded)
                actionable = matches - would_skip

                actions = []
                if ll.assign_enabled:
                    actions.append("assign")
                if ll.workflow_enabled:
                    actions.append("workflow")
                action_str = "+".join(actions) or "none"

                marker = "✓" if actionable == 0 else "!"
                click.echo(
                    f"{marker} {ll.id}  org={ll.close_org_id[:20]}  {ll.name!r}"
                )
                click.echo(
                    f"    since {after_dt.isoformat(timespec='seconds')}  "
                    f"actions={action_str}  matches={matches}  "
                    f"would_skip={would_skip}  actionable={actionable}"
                )

                if actionable > 0:
                    lists_with_backlog += 1
                    total_actionable += actionable

            except CloseAPIError as e:
                click.echo(f"! {ll.id} {ll.name!r} — Close API error: {e}", err=True)
            except Exception as e:
                click.echo(f"! {ll.id} {ll.name!r} — error: {e}", err=True)

        click.echo(
            f"\nSummary: {lists_with_backlog} list(s) have a backlog, "
            f"{total_actionable} lead(s) total would be acted on."
        )

    @app.cli.command("reseed-all-active")
    @click.option("--dry-run", is_flag=True, help="List what would be re-seeded without changing anything.")
    def reseed_all_active(dry_run):
        """
        Safely resume polling after an outage: for every active Lead List,
        re-seed its "already seen" set to the current state in Close.

        Meant for use after Robin was unavailable long enough that customers
        may have handled the intervening leads manually. Without this, the
        next poll would process the whole backlog — potentially overwriting
        manual assignments or firing workflows on already-handled leads.

        Per list: pause -> re-seed -> resume, one at a time. A single list's
        failure does not stop the others. Run with --dry-run first to see
        what will be touched.
        """
        from .models.lead_list import LeadList, STATUS_ACTIVE, STATUS_PAUSED
        from .services.assignment_engine import seed_queue

        active = (
            LeadList.query
            .filter_by(status=STATUS_ACTIVE)
            .order_by(LeadList.close_org_id, LeadList.name)
            .all()
        )

        if not active:
            click.echo("No active Lead Lists found. Nothing to do.")
            return

        click.echo(f"Found {len(active)} active Lead List(s).")
        if dry_run:
            for ll in active:
                click.echo(f"  [dry-run] would re-seed  {ll.id}  org={ll.close_org_id}  {ll.name!r}")
            click.echo("\nDry run complete. Re-run without --dry-run to apply.")
            return

        succeeded = 0
        failed = 0
        for ll in active:
            click.echo(f"→ {ll.id}  org={ll.close_org_id}  {ll.name!r}")
            try:
                # 1. Pause so the scheduler cannot poll this list mid-reseed.
                ll.status = STATUS_PAUSED
                db.session.commit()

                # 2. Re-seed: fetch every currently-matching lead and mark it seen.
                seed_queue(ll.id)

                # 3. Refresh from DB (seed_queue may have committed) and resume.
                db.session.refresh(ll)
                ll.status = STATUS_ACTIVE
                db.session.commit()
                click.echo(f"    ✓ re-seeded ({len(ll.seeded_lead_ids or [])} lead(s) snapshotted)")
                succeeded += 1
            except Exception as exc:
                # Keep going — one bad list should not block the rest. Leave it
                # paused so a broken list does not silently start assigning.
                db.session.rollback()
                click.echo(f"    ✗ FAILED: {exc}", err=True)
                click.echo(f"      (Lead List left paused — investigate and resume manually.)", err=True)
                failed += 1

        click.echo(f"\nDone. {succeeded} re-seeded, {failed} failed.")
        if failed:
            raise SystemExit(1)

    return app
