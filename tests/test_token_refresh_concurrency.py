"""
Real-concurrency proof for token-refresh serialization — requires Postgres
(the ``SELECT ... FOR UPDATE`` row lock is a no-op on SQLite, so this can't run
there). Skipped unless ``TEST_DATABASE_URL`` points at a throwaway Postgres, e.g.:

    docker run -d --name pg -e POSTGRES_PASSWORD=test -e POSTGRES_DB=robintest \
        -p 55432:5432 postgres:16
    TEST_DATABASE_URL=postgresql://postgres:test@localhost:55432/robintest \
        python -m unittest tests.test_token_refresh_concurrency -v

It launches several threads that all try to refresh the SAME user's
already-expired token at once and asserts Close's refresh endpoint is called
exactly once — proving a rotating refresh token is never spent twice.
"""
import os
import threading
import unittest
from datetime import datetime, timedelta

TEST_DB = os.environ.get("TEST_DATABASE_URL")


@unittest.skipUnless(TEST_DB, "set TEST_DATABASE_URL to a throwaway Postgres to run")
class TokenRefreshConcurrencyTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ["DATABASE_URL"] = TEST_DB
        os.environ.setdefault("SECRET_KEY", "test-secret")
        from app import create_app
        from app.extensions import db
        cls.app = create_app()
        cls.db = db
        with cls.app.app_context():
            db.drop_all()
            db.create_all()

    def setUp(self):
        from app.models.user import User
        from app.services import close_api
        self.close_api = close_api
        with self.app.app_context():
            self.db.session.query(User).delete()
            u = User(
                close_user_id="user_race", close_org_id="orga_race",
                email="race@example.com", access_token="A1", refresh_token="R1",
                token_expires_at=datetime.utcnow() - timedelta(seconds=10),
            )
            self.db.session.add(u)
            self.db.session.commit()
            self.uid = u.id
        # SQLite-style writable probe would say False on some setups; force True.
        self._orig_writable = close_api._db_is_writable
        close_api._db_is_writable = lambda: True
        self._orig_refresh = close_api.refresh_access_token

    def tearDown(self):
        self.close_api._db_is_writable = self._orig_writable
        self.close_api.refresh_access_token = self._orig_refresh

    def test_concurrent_refreshers_spend_the_token_once(self):
        from app.models.user import User
        from app.services.close_api import CloseClient

        calls = []
        calls_lock = threading.Lock()
        start = threading.Barrier(5)

        def fake_refresh(refresh_token):
            with calls_lock:
                calls.append(refresh_token)
                n = len(calls)
            # Hold the "network" open so every thread piles onto the row lock
            # while the winner is still refreshing — this is the race window.
            import time
            time.sleep(0.5)
            return {"access_token": f"A{n+1}", "refresh_token": f"R{n+1}", "expires_in": 3600}

        self.close_api.refresh_access_token = fake_refresh

        errors = []

        def worker():
            try:
                with self.app.app_context():
                    # Each thread loads its own (stale, expired) view of the user,
                    # exactly like the poller and a web request would.
                    u = self.db.session.query(User).filter_by(id=self.uid).one()
                    start.wait(timeout=10)
                    CloseClient(u)._ensure_fresh_token()
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)

        self.assertEqual(errors, [], f"workers raised: {errors}")
        # The crux: exactly ONE call to Close, with the original refresh token.
        self.assertEqual(calls, ["R1"], f"expected a single refresh, got {calls}")

        with self.app.app_context():
            row = self.db.session.query(User).filter_by(id=self.uid).one()
            self.assertEqual(row.access_token, "A2")
            self.assertEqual(row.refresh_token, "R2")
            self.assertGreater(
                row.token_expires_at, datetime.utcnow() + timedelta(seconds=3000)
            )


if __name__ == "__main__":
    unittest.main()
