"""
Regression tests for OAuth token-refresh atomicity/serialization.

Background: Close rotates the refresh token on every refresh and invalidates
the old one immediately. A rotated token that is spent twice (two workers
refreshing the same user) or not persisted atomically gets rejected with
``invalid_grant`` on next use — "burning" the org's connection. These tests
pin the guarantees in ``CloseClient._refresh_token_locked``.

Run with:  python -m unittest discover -s tests
(No pytest dependency — stdlib unittest, temp file-based SQLite.)
"""
import os
import tempfile
import unittest
from datetime import datetime, timedelta

os.environ.setdefault("SECRET_KEY", "test-secret")

# A file-based SQLite DB (NOT :memory:) so the independent Session opened via
# db.engine during a refresh shares the same database as the request session.
_DB_FD, _DB_PATH = tempfile.mkstemp(suffix=".sqlite")
os.environ["DATABASE_URL"] = f"sqlite:///{_DB_PATH}"

from app import create_app  # noqa: E402
from app.extensions import db  # noqa: E402
from app.models.user import User  # noqa: E402
from app.services import close_api  # noqa: E402
from app.services.close_api import CloseClient, CloseAPIError  # noqa: E402

_app = create_app()
_ctx = _app.app_context()
_ctx.push()
db.create_all()


def tearDownModule():
    db.session.remove()
    _ctx.pop()
    os.close(_DB_FD)
    try:
        os.remove(_DB_PATH)
    except OSError:
        pass


class TokenRefreshTestCase(unittest.TestCase):
    def setUp(self):
        db.session.query(User).delete()
        db.session.commit()
        # In tests the DB is SQLite; the real read-only probe (Postgres-only)
        # would report "not writable", so treat it as writable except where a
        # test overrides it.
        self._orig_writable = close_api._db_is_writable
        close_api._db_is_writable = lambda: True
        self._orig_refresh = close_api.refresh_access_token

    def tearDown(self):
        close_api._db_is_writable = self._orig_writable
        close_api.refresh_access_token = self._orig_refresh
        db.session.rollback()

    # ---- helpers ---------------------------------------------------------
    def _make_user(self, *, access="A1", refresh="R1", expires_delta_s=-10):
        expires = (
            None if expires_delta_s is None
            else datetime.utcnow() + timedelta(seconds=expires_delta_s)
        )
        u = User(
            close_user_id="user_x", close_org_id="orga_x", email="x@example.com",
            access_token=access, refresh_token=refresh, token_expires_at=expires,
        )
        db.session.add(u)
        db.session.commit()
        return u

    def _db_user(self):
        """Re-read the row from the DB, bypassing the identity map."""
        db.session.expire_all()
        return db.session.query(User).filter_by(close_user_id="user_x").one()

    def _stub_refresh(self, result=None, exc=None):
        calls = []

        def fake(refresh_token):
            calls.append(refresh_token)
            if exc is not None:
                raise exc
            return result

        close_api.refresh_access_token = fake
        return calls

    # ---- tests -----------------------------------------------------------
    def test_stale_token_is_refreshed_and_persisted_atomically(self):
        u = self._make_user(access="A1", refresh="R1", expires_delta_s=-10)
        calls = self._stub_refresh(
            {"access_token": "A2", "refresh_token": "R2", "expires_in": 3600}
        )
        client = CloseClient(u)
        client._ensure_fresh_token()

        self.assertEqual(calls, ["R1"], "Close refresh should be called once with old token")
        # Persisted to the DB…
        row = self._db_user()
        self.assertEqual(row.access_token, "A2")
        self.assertEqual(row.refresh_token, "R2")
        self.assertGreater(row.token_expires_at, datetime.utcnow() + timedelta(seconds=3000))
        # …and mirrored onto the in-memory user for the current request.
        self.assertEqual(client.user.access_token, "A2")
        self.assertEqual(client.user.refresh_token, "R2")

    def test_fresh_token_does_not_call_close(self):
        u = self._make_user(expires_delta_s=3600)
        calls = self._stub_refresh(exc=AssertionError("refresh must not be called"))
        CloseClient(u)._ensure_fresh_token()
        self.assertEqual(calls, [])

    def test_none_expiry_never_refreshes(self):
        u = self._make_user(expires_delta_s=None)
        calls = self._stub_refresh(exc=AssertionError("refresh must not be called"))
        CloseClient(u)._ensure_fresh_token()
        self.assertEqual(calls, [])

    def test_second_worker_adopts_token_instead_of_respending(self):
        # DB row is already freshly rotated (as if another worker just won the
        # lock and refreshed): R2 / far-future expiry.
        self._make_user(access="A2", refresh="R2", expires_delta_s=3600)
        # This client holds a STALE in-memory view (what a racing worker's
        # object looks like): old token, already-expired.
        stale = self._db_user()
        stale.access_token = "A1"
        stale.refresh_token = "R1"
        stale.token_expires_at = datetime.utcnow() - timedelta(seconds=10)

        calls = self._stub_refresh(exc=AssertionError("must NOT re-spend the refresh token"))
        client = CloseClient(stale)
        client._ensure_fresh_token()

        # It re-read under the lock, saw a fresh token, and adopted it — no
        # second call to Close (which would have burned the rotated token).
        self.assertEqual(calls, [])
        self.assertEqual(client.user.access_token, "A2")
        self.assertEqual(client.user.refresh_token, "R2")

    def test_read_only_db_skips_refresh_and_keeps_token(self):
        u = self._make_user(access="A1", refresh="R1", expires_delta_s=-10)
        close_api._db_is_writable = lambda: False
        calls = self._stub_refresh(exc=AssertionError("refresh must not be called"))

        with self.assertRaises(CloseAPIError) as cm:
            CloseClient(u)._ensure_fresh_token()
        self.assertIn("read-only", str(cm.exception).lower())
        self.assertEqual(calls, [])
        row = self._db_user()
        self.assertEqual(row.refresh_token, "R1", "token must be left intact for later")

    def test_invalid_grant_preserves_stored_token_and_raises(self):
        u = self._make_user(access="A1", refresh="R1", expires_delta_s=-10)
        self._stub_refresh(exc=CloseAPIError("Token refresh failed: HTTP 400 — invalid_grant",
                                             status_code=400))
        with self.assertRaises(CloseAPIError):
            CloseClient(u)._ensure_fresh_token()
        # No half-rotation written: the stored token is unchanged.
        row = self._db_user()
        self.assertEqual(row.access_token, "A1")
        self.assertEqual(row.refresh_token, "R1")

    def test_missing_refresh_token_raises(self):
        u = self._make_user(refresh=None, expires_delta_s=-10)
        self._stub_refresh(exc=AssertionError("refresh must not be called"))
        with self.assertRaises(CloseAPIError):
            CloseClient(u)._ensure_fresh_token()


if __name__ == "__main__":
    unittest.main()
