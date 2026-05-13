<div align="center">
  <img src="app/static/img/robin-logo.png" width="96" alt="Robin" style="border-radius:18px;">
  <h1>Robin</h1>
  <p><em>the faithful distributor</em></p>
  <p>Round-robin lead assignment for <a href="https://close.com">Close CRM</a>.</p>
</div>

---

Robin connects to your Close organization, watches for new leads that match filters you define, and runs configured Actions on each match — round-robin-assigning the lead to a teammate, triggering a Close Workflow on the lead's first contact, or both. You define **Lead Lists** (filter + Actions) and **Groups** (rotation rosters used by the Assign action). Robin polls Close every five minutes.

> **Note:** Robin is an independent tool and is not officially affiliated with or supported by Close.

## Features

- **Lead Lists with configurable Actions** — each list pairs a Close Advanced Filter with one or both Actions: round-robin assignment to a Group, and/or triggering a Close Workflow on the lead's first contact
- **Round-robin assignment** — leads are distributed evenly across your team; a single Group can power many Lead Lists, sharing one rotation pointer
- **Workflow triggering** — enroll matching leads in any manually-triggerable Close Workflow, with a per-list "run as" user whose connected email account sends email steps
- **Continuous monitoring** — polls Close every 5 minutes using the Advanced Filter API
- **Activity Log** — full audit trail of every assignment and workflow trigger, with filters by Group, Lead List, user, timeframe, and lead search; workflow triggers deep-link to the run in Close
- **Multi-org support** — one login can manage multiple Close organizations
- **Admin controls** — approve users, manage roles, toggle member active/inactive status

## Running locally

### Prerequisites

- Python 3.11+
- PostgreSQL
- A [Close OAuth app](https://app.close.com/settings/developer/) with a redirect URI of `http://localhost:5001/auth/callback`

### Setup

```bash
# Clone the repo
git clone git@github.com:nickpersico/robin.git
cd robin

# Create and activate a virtual environment
python3 -m venv venv
source venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Create a local database
createdb robin

# Copy the example env file and fill in your values
cp .env.example .env
```

Edit `.env`:

```env
SECRET_KEY=any-long-random-string
DATABASE_URL=postgresql://localhost/robin

CLOSE_CLIENT_ID=your_close_client_id
CLOSE_CLIENT_SECRET=your_close_client_secret
CLOSE_REDIRECT_URI=http://localhost:5001/auth/callback
```

### Run

```bash
# Apply database migrations
flask db upgrade

# Start the dev server
python run.py
```

The app will be at [http://localhost:5001](http://localhost:5001). Sign in with your Close account — the first user is automatically promoted to admin.

## Deploying to Fly.io

### Prerequisites

- [flyctl](https://fly.io/docs/hands-on/install-flyctl/) installed and authenticated (`fly auth login`)
- A Close OAuth app with a redirect URI of `https://<your-app-name>.fly.dev/auth/callback`

### Steps

```bash
# 1. Create the app
fly apps create robin

# 2. Create and attach a Postgres database
fly postgres create --name robin-db --region iad --vm-size shared-cpu-1x --volume-size 1
fly postgres attach robin-db --app robin

# 3. Set secrets
fly secrets set \
  SECRET_KEY="$(python3 -c 'import secrets; print(secrets.token_hex(32))')" \
  CLOSE_CLIENT_ID="your_close_client_id" \
  CLOSE_CLIENT_SECRET="your_close_client_secret" \
  CLOSE_REDIRECT_URI="https://robin.closekit.com/auth/callback"

# 4. Deploy
fly deploy
```

`start.sh` runs `flask db upgrade` automatically before each deploy, so schema migrations are always applied.

### Promoting yourself to admin

After signing in for the first time, Robin will show a "pending approval" screen. Since there are no admins yet, run this from your local machine (pointed at the production database) or via `fly ssh console`:

```bash
flask make-admin your@email.com
```

### Custom domain

```bash
fly certs add robin.yourdomain.com
```

Then add a CNAME record pointing to `robin-closekit.fly.dev` and update `CLOSE_REDIRECT_URI` in your Close OAuth app settings and in Fly secrets.

## Environment variables

| Variable | Description |
|---|---|
| `SECRET_KEY` | Flask session secret — use a long random string in production |
| `DATABASE_URL` | PostgreSQL connection string |
| `CLOSE_CLIENT_ID` | OAuth app client ID from Close developer settings |
| `CLOSE_CLIENT_SECRET` | OAuth app client secret |
| `CLOSE_REDIRECT_URI` | Must exactly match the redirect URI registered in Close |

## Tech stack

- [Flask](https://flask.palletsprojects.com/) + [SQLAlchemy](https://www.sqlalchemy.org/) + [Flask-Migrate](https://flask-migrate.readthedocs.io/)
- [PostgreSQL](https://www.postgresql.org/)
- [APScheduler](https://apscheduler.readthedocs.io/) — background polling every 5 minutes
- [Close Advanced Filter API](https://developer.close.com/)
- Plain HTML/CSS — no frontend framework

## License

MIT
