"""Configuration, read once from the environment at import time.

Every knob the app has lives here so there is exactly one place to look when
something is misconfigured. See .env.example for a template.
"""

import os

from dotenv import load_dotenv

load_dotenv()


def _str(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _int(name: str, default: int) -> int:
    raw = _str(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer, got {raw!r}") from exc


def _bool(name: str, default: bool) -> bool:
    raw = _str(name).lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def _csv(name: str) -> set[str]:
    return {part.strip() for part in _str(name).split(",") if part.strip()}


def _normalize_database_url(url: str) -> str:
    """Railway (and Heroku) hand out ``postgres://``; SQLAlchemy wants a driver."""
    if url.startswith("postgres://"):
        return url.replace("postgres://", "postgresql+psycopg://", 1)
    if url.startswith("postgresql://"):
        return url.replace("postgresql://", "postgresql+psycopg://", 1)
    return url


# --- Secrets ---------------------------------------------------------------
# Stripped because copy-pasting a key into a dashboard often brings a newline
# with it, which SMTP and the Anthropic client both reject in confusing ways.
ANTHROPIC_API_KEY = _str("ANTHROPIC_API_KEY")
APP_PASSWORD = _str("APP_PASSWORD")
SECRET_KEY = _str("SECRET_KEY")

GMAIL_USER = _str("GMAIL_USER")
GMAIL_APP_PASSWORD = _str("GMAIL_APP_PASSWORD")
GMAIL_ALLOWED_RECIPIENTS = _csv("GMAIL_ALLOWED_RECIPIENTS")

# --- Storage ---------------------------------------------------------------
# SQLite by default so a fresh clone runs with no setup. Set DATABASE_URL to a
# Postgres URL in deployment: container filesystems do not survive a redeploy,
# so a file-backed database there silently loses every agent and message.
DATABASE_URL = _normalize_database_url(_str("DATABASE_URL") or "sqlite:///chat_history.db")

# --- Model -----------------------------------------------------------------
CHAT_MODEL = _str("CHAT_MODEL") or "claude-opus-5"
# max_tokens caps thinking *and* visible reply together. Current models think by
# default, so a tight budget truncates the answer mid-sentence.
MAX_TOKENS = _int("MAX_TOKENS", 8192)
# Thinking depth / token spend: low | medium | high | xhigh | max.
# "medium" keeps a chat app responsive; raise it for harder reasoning.
EFFORT = _str("EFFORT") or "medium"
# Turns of history sent per request. The API is stateless, so the whole
# conversation is re-sent (and re-billed) every time; without a cap, cost grows
# quadratically with conversation length.
HISTORY_LIMIT = _int("HISTORY_LIMIT", 40)
# Ceiling on tool-call rounds in a single turn, so a model that keeps reaching
# for tools cannot loop forever.
MAX_TOOL_ROUNDS = _int("MAX_TOOL_ROUNDS", 6)

# --- Web -------------------------------------------------------------------
PORT = _int("PORT", 8080)
ALLOWED_ORIGINS = _csv("ALLOWED_ORIGINS")
# Send the session cookie only over HTTPS. Its own flag rather than inferred
# from ALLOWED_ORIGINS, which is unrelated and optional.
SECURE_COOKIES = _bool("SECURE_COOKIES", False)
SESSION_DAYS = _int("SESSION_DAYS", 7)
# Failed logins allowed per client address per window, before a 429.
LOGIN_MAX_ATTEMPTS = _int("LOGIN_MAX_ATTEMPTS", 10)
LOGIN_WINDOW_S = _int("LOGIN_WINDOW_S", 60)


def validate() -> None:
    """Fail closed on boot rather than serving something dangerous.

    This app can send email as its operator. An instance without a password is
    an open relay for anyone who finds the URL, so a missing APP_PASSWORD is a
    hard startup failure, not a warning.
    """
    if not APP_PASSWORD:
        raise RuntimeError(
            "APP_PASSWORD is not set. This app can send email on your behalf, so it "
            "refuses to start without a password. Set APP_PASSWORD in your .env "
            "(see .env.example) or in your host's environment variables."
        )
    if len(APP_PASSWORD) < 8:
        raise RuntimeError("APP_PASSWORD must be at least 8 characters.")
    if not SECRET_KEY:
        raise RuntimeError(
            "SECRET_KEY is not set. It signs your login cookie, and with more than one "
            "server worker an unset key means each worker signs differently — you would "
            "be logged out at random. Generate one with:\n"
            '  python -c "import secrets; print(secrets.token_hex(32))"'
        )
