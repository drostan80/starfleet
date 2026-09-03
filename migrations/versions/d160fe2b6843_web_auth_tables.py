"""Web auth tables — login gate for the HTML client (2026-09-03).

Adds three tables for the web UI login/password gate:

- `web_user` — bcrypt-hashed credentials, managed via CLI
  (`lcars add-user`, `lcars reset-password`).
- `web_session` — server-side sessions (httpOnly cookie), 30-day sliding
  TTL, validated by nginx auth_request → LCARS `/auth/check`.
- `web_setting` — shared settings (LCARS URL, bearer token, home server
  host, TMDB API key) that survive across browsers/devices, as opposed
  to machine-specific settings (mpv helper URL) which stay in
  localStorage.
"""

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d160fe2b6843"
down_revision: str | None = "d4e5f6071829"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute("""
        CREATE TABLE web_user (
            id         TEXT PRIMARY KEY CHECK (length(id) = 8 AND substr(id, 1, 2) = 'u-'),
            username   TEXT NOT NULL UNIQUE COLLATE NOCASE,
            password   TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
    op.execute("""
        CREATE TABLE web_session (
            token      TEXT PRIMARY KEY,
            user_id    TEXT NOT NULL REFERENCES web_user (id),
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL
        )
    """)
    op.execute("CREATE INDEX ix_web_session_user ON web_session (user_id)")
    op.execute("CREATE INDEX ix_web_session_expires ON web_session (expires_at)")
    op.execute("""
        CREATE TABLE web_setting (
            key   TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
    """)


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS web_setting")
    op.execute("DROP TABLE IF EXISTS web_session")
    op.execute("DROP TABLE IF EXISTS web_user")
