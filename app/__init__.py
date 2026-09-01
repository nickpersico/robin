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

    # ── Error handlers ───────────────────────────────────────────────────────
    # Give users a branded page instead of the bare browser error, and record
    # every one to the error_logs table so support can triage them from
    # /system. Recording is best-effort and never raises (see error_logging).
    from flask import render_template
    from flask_login import current_user as _current_user
    from .services.error_logging import log_error

    @app.errorhandler(404)
    def handle_not_found(exc):
        # Log 404s only for signed-in users. Anonymous 404s are dominated by
        # bot/scanner traffic and would bury real customer issues; a signed-in
        # user hitting a 404 usually means a broken link or a stale URL.
        if getattr(_current_user, "is_authenticated", False):
            log_error(404)
        return render_template("errors/404.html"), 404

    @app.errorhandler(Exception)
    def handle_unexpected_error(exc):
        # Let Flask handle other HTTP errors (403, redirects, etc.) normally;
        # only intercept genuine unhandled exceptions as 500s.
        from werkzeug.exceptions import HTTPException

        if isinstance(exc, HTTPException):
            return exc

        # Roll back first so a poisoned transaction doesn't linger on this
        # worker, then record the error on a fresh transaction.
        db.session.rollback()
        app.logger.exception("Unhandled exception")
        log_error(500, exc)
        return render_template("errors/500.html"), 500

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
    @click.option("--email", help="Look up the org by any user's email.")
    @click.option("--org-id", help="Look up the org by Close org id (orga_...).")
    def inspect_org(email, org_id):
        """
        Dump a Close org's Robin configuration for support triage.

        Given an email OR a close_org_id, prints:
          - the matched user + role/status
          - every Group in the org with members (position, active flag,
            name, email)
          - every Lead List in the org with its status, actions, target
            field, rotation link, and overwrite setting

        Meant for answering "what is customer X actually configured to do?"
        without SSHing in and writing an ad-hoc script each time.
        """
        from sqlalchemy import func as _func
        from .models.user import User
        from .models.rotation import Rotation
        from .models.lead_list import LeadList

        if not email and not org_id:
            click.echo("Provide either --email or --org-id.", err=True)
            raise SystemExit(2)

        resolved_org_id = org_id
        if email:
            user = (
                User.query
                .filter(_func.lower(User.email) == email.lower())
                .order_by(User.created_at)
                .first()
            )
            if user is None:
                click.echo(f"No user found with email: {email}", err=True)
                raise SystemExit(1)
            resolved_org_id = user.close_org_id
            full_name = f"{user.first_name or ''} {user.last_name or ''}".strip() or "(no name)"
            click.echo(
                f"User:  {user.email}  ({full_name})  "
                f"role={user.role}  status={user.status}"
            )

        click.echo(f"Org:   {resolved_org_id}")

        rotations = (
            Rotation.query
            .filter_by(close_org_id=resolved_org_id)
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
            .filter_by(close_org_id=resolved_org_id)
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
        from .services.close_api import CloseAPIError
        from .services.assignment_engine import (
            _normalize_filter,
            _inject_date_filter,
            _get_org_client,
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
                client = _get_org_client(ll.close_org_id)
                if client is None:
                    click.echo(f"! {ll.id} {ll.name!r} — no usable Close connection for org, skipped")
                    continue

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

    @app.cli.command("dedup-assignment-logs")
    @click.option("--apply", is_flag=True, help="Actually delete rows. Without this flag it's a dry run.")
    @click.option("--batch", default=10000, show_default=True, help="Max rows to delete per statement.")
    def dedup_assignment_logs(apply, batch):
        """
        Remove duplicate AssignmentLog rows, keeping the earliest per
        (queue_id, close_lead_id).

        Works one Lead List at a time, deleting in bounded batches and
        committing after each, so it stays light on the database and is safe to
        interrupt/resume. Dry run by default — pass --apply to delete.

        A row is deleted only if an earlier row exists for the same list + lead
        (earlier assigned_at, ties broken by id), so exactly one — the first —
        assignment per lead survives.
        """
        from sqlalchemy import text
        from .models.lead_list import LeadList

        lead_lists = LeadList.query.order_by(LeadList.created_at).all()

        delete_batch_sql = text("""
            DELETE FROM assignment_logs
            WHERE id IN (
                SELECT a.id FROM assignment_logs a
                WHERE a.queue_id = :q
                  AND EXISTS (
                      SELECT 1 FROM assignment_logs b
                      WHERE b.queue_id = a.queue_id
                        AND b.close_lead_id = a.close_lead_id
                        AND (b.assigned_at < a.assigned_at
                             OR (b.assigned_at = a.assigned_at AND b.id < a.id))
                  )
                LIMIT :batch
            )
        """)

        total_deleted = 0
        for ll in lead_lists:
            rows = db.session.execute(
                text("SELECT count(*) FROM assignment_logs WHERE queue_id = :q"),
                {"q": ll.id},
            ).scalar()
            if not rows:
                continue

            if not apply:
                click.echo(f"[dry-run] {ll.id} {ll.name!r}: {rows} row(s) — keep 1 per lead")
                continue

            deleted_here = 0
            while True:
                res = db.session.execute(delete_batch_sql, {"q": ll.id, "batch": batch})
                db.session.commit()
                if res.rowcount == 0:
                    break
                deleted_here += res.rowcount
                click.echo(f"  {ll.id}: deleted {deleted_here} duplicate(s) so far…")
            total_deleted += deleted_here
            if deleted_here:
                click.echo(f"✓ {ll.id} {ll.name!r}: removed {deleted_here} duplicate(s)")

        if apply:
            click.echo(f"\nDone. Removed {total_deleted} duplicate row(s).")
            click.echo("Run `VACUUM (FULL, ANALYZE) assignment_logs;` afterward to reclaim disk.")
        else:
            click.echo("\nDry run complete. Re-run with --apply to delete.")

    @app.cli.command("prune-assignment-logs")
    @click.option("--months", default=12, show_default=True, help="Delete rows older than this many months.")
    @click.option("--apply", is_flag=True, help="Actually delete. Without this it's a dry run.")
    @click.option("--batch", default=10000, show_default=True, help="Max rows to delete per statement.")
    def prune_assignment_logs(months, apply, batch):
        """
        Retention: delete AssignmentLog rows older than --months, in bounded
        batches with a commit after each (uses the assigned_at index). Dry run
        by default. Keeps the table from growing without bound over time.
        """
        from datetime import datetime, timedelta
        from sqlalchemy import text

        cutoff = datetime.utcnow() - timedelta(days=30 * months)
        total = db.session.execute(
            text("SELECT count(*) FROM assignment_logs WHERE assigned_at < :c"),
            {"c": cutoff},
        ).scalar()
        click.echo(f"Rows older than {months} month(s) (before {cutoff.date()}): {total}")
        if not total:
            click.echo("Nothing to prune.")
            return
        if not apply:
            click.echo("Dry run — re-run with --apply to delete.")
            return

        delete_sql = text("""
            DELETE FROM assignment_logs
            WHERE id IN (
                SELECT id FROM assignment_logs WHERE assigned_at < :c LIMIT :b
            )
        """)
        deleted = 0
        while True:
            res = db.session.execute(delete_sql, {"c": cutoff, "b": batch})
            db.session.commit()
            if res.rowcount == 0:
                break
            deleted += res.rowcount
            click.echo(f"  deleted {deleted}…")
        click.echo(f"\nDone. Removed {deleted} row(s).")

    @app.cli.command("connection-status")
    def connection_status():
        """
        For every org with an active Lead List, show whether Robin can currently
        reach Close (i.e. whether its OAuth connection still works). Handy for
        tracking who still needs to re-authenticate after a token reset.
        """
        from .models.lead_list import LeadList
        from .models.organization import Organization
        from .services.assignment_engine import _get_org_client

        orgs = {
            r[0] for r in db.session.query(LeadList.close_org_id)
            .filter_by(status="active").distinct() if r[0]
        }
        names = dict(db.session.query(Organization.close_org_id, Organization.name).all())
        connected = 0
        for org in sorted(orgs, key=lambda o: names.get(o) or o):
            try:
                ok = _get_org_client(org) is not None
            except Exception:
                ok = False
            if ok:
                connected += 1
            click.echo(f"  {'CONNECTED' if ok else 'burned   '}  {names.get(org) or org}")
        click.echo(f"\n{connected}/{len(orgs)} org(s) connected.")

    return app
