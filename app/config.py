import os
from dotenv import load_dotenv

load_dotenv()


class Config:
    SECRET_KEY = os.environ.get("SECRET_KEY", "dev-secret-change-me")
    _db_url = os.environ.get("DATABASE_URL", "postgresql://localhost/robin")
    # Fly.io provides postgres:// which SQLAlchemy 2.x requires as postgresql://
    if _db_url.startswith("postgres://"):
        _db_url = _db_url.replace("postgres://", "postgresql://", 1)
    SQLALCHEMY_DATABASE_URI = _db_url
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    CLOSE_CLIENT_ID = os.environ.get("CLOSE_CLIENT_ID")
    CLOSE_CLIENT_SECRET = os.environ.get("CLOSE_CLIENT_SECRET")
    CLOSE_REDIRECT_URI = os.environ.get("CLOSE_REDIRECT_URI")

    CLOSE_AUTHORIZE_URL = "https://app.close.com/oauth2/authorize/"
    CLOSE_TOKEN_URL = "https://api.close.com/oauth2/token/"
    CLOSE_REVOKE_URL = "https://api.close.com/oauth2/revoke/"
    CLOSE_API_BASE = "https://api.close.com/api/v1"
