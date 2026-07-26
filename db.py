"""Storage layer.

Everything durable lives here: agents, chat messages, and in-flight chat jobs.

One codepath serves both SQLite (local, zero setup) and Postgres (deployment),
selected by DATABASE_URL. The important property is that *none* of this is in
process memory: a container restart or a second gunicorn worker must not lose
or hide state.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    create_engine,
    delete,
    insert,
    select,
    update,
)

import config

metadata = MetaData()

agents = Table(
    "agents",
    metadata,
    Column("id", String(36), primary_key=True),
    Column("name", String(200), nullable=False),
    Column("type", String(50), nullable=False),
    Column("instructions", Text, nullable=False),
    Column("status", String(20), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
)

messages = Table(
    "messages",
    metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("agent_id", String(36), ForeignKey("agents.id", ondelete="CASCADE"), nullable=False),
    Column("role", String(20), nullable=False),
    Column("content", Text, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
)

# In-flight chat turns. These were previously a module-level dict, which meant
# they vanished on restart and were invisible to sibling gunicorn workers (a
# poll could land on a process that had never heard of the job).
jobs = Table(
    "jobs",
    metadata,
    Column("id", String(36), primary_key=True),
    Column("agent_id", String(36), nullable=False),
    Column("status", String(30), nullable=False),  # pending|done|error|awaiting_confirmation
    Column("reply", Text, nullable=True),
    Column("error", Text, nullable=True),
    # Pending send_email payload, shown to the user for confirmation.
    Column("pending_email", JSON, nullable=True),
    # Single-use token identifying *this specific* parked message. Approval is
    # bound to the token, not just the job id — otherwise a replayed confirm
    # approves whatever happens to be parked when it lands, which can be a
    # different email the user never saw.
    Column("pending_token", String(36), nullable=True),
    # Serialized conversation state so a confirmed/cancelled turn can resume in
    # whichever worker process picks it up.
    Column("conversation", JSON, nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
)

# Records one-time migrations so they are not re-run. Inferring "already done"
# from the state of other tables is what made the legacy import repeat.
migrations = Table(
    "migrations",
    metadata,
    Column("name", String(100), primary_key=True),
    Column("applied_at", DateTime(timezone=True), nullable=False),
)

LEGACY_IMPORT_MARKER = "import_legacy_bots_json"
MAX_LEGACY_ENTRIES = 500

_engine = None


def _now() -> datetime:
    return datetime.now(UTC)


def _migration_done(name: str) -> bool:
    with get_engine().connect() as conn:
        return (
            conn.execute(select(migrations.c.name).where(migrations.c.name == name)).fetchone()
            is not None
        )


def _mark_migration_done(name: str) -> None:
    try:
        with get_engine().begin() as conn:
            conn.execute(insert(migrations).values(name=name, applied_at=_now()))
    except Exception:
        # Another worker booting concurrently won the insert. Harmless.
        pass


def get_engine():
    global _engine
    if _engine is None:
        kwargs: dict[str, Any] = {"future": True, "pool_pre_ping": True}
        if config.DATABASE_URL.startswith("sqlite"):
            # Background threads handle chat turns, so the connection must not be
            # pinned to the creating thread.
            kwargs["connect_args"] = {"check_same_thread": False}
        _engine = create_engine(config.DATABASE_URL, **kwargs)
    return _engine


def init_db() -> None:
    metadata.create_all(get_engine())
    _import_legacy_bots_json()


# --- Agents ----------------------------------------------------------------


def _agent_row_to_dict(row) -> dict[str, Any]:
    return {
        "id": row.id,
        "name": row.name,
        "type": row.type,
        "instructions": row.instructions,
        "status": row.status,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


def list_agents() -> list[dict[str, Any]]:
    with get_engine().connect() as conn:
        rows = conn.execute(select(agents).order_by(agents.c.created_at)).fetchall()
    return [_agent_row_to_dict(r) for r in rows]


def get_agent(agent_id: str) -> dict[str, Any] | None:
    with get_engine().connect() as conn:
        row = conn.execute(select(agents).where(agents.c.id == agent_id)).fetchone()
    return _agent_row_to_dict(row) if row else None


def create_agent(name: str, agent_type: str = "bot", instructions: str = "") -> dict[str, Any]:
    # uuid4, not len(agents)+1: the old scheme reused an ID after a delete, so a
    # new agent could collide with an existing one and inherit its chat history.
    agent = {
        "id": str(uuid.uuid4()),
        "name": name,
        "type": agent_type or "bot",
        "instructions": instructions or "",
        "status": "idle",
        "created_at": _now(),
    }
    with get_engine().begin() as conn:
        conn.execute(insert(agents).values(**agent))
    agent["created_at"] = agent["created_at"].isoformat()
    return agent


def delete_agent(agent_id: str) -> None:
    with get_engine().begin() as conn:
        conn.execute(delete(messages).where(messages.c.agent_id == agent_id))
        conn.execute(delete(jobs).where(jobs.c.agent_id == agent_id))
        conn.execute(delete(agents).where(agents.c.id == agent_id))


def set_agent_status(agent_id: str, status: str) -> None:
    with get_engine().begin() as conn:
        conn.execute(update(agents).where(agents.c.id == agent_id).values(status=status))


# --- Messages --------------------------------------------------------------


def add_message(agent_id: str, role: str, content: str) -> None:
    with get_engine().begin() as conn:
        conn.execute(
            insert(messages).values(
                agent_id=agent_id, role=role, content=content, created_at=_now()
            )
        )


def list_messages(agent_id: str, limit: int | None = None) -> list[dict[str, Any]]:
    """Return messages oldest-first. With ``limit``, return the most recent N."""
    stmt = select(messages).where(messages.c.agent_id == agent_id)
    if limit is None:
        stmt = stmt.order_by(messages.c.id)
        with get_engine().connect() as conn:
            rows = conn.execute(stmt).fetchall()
    else:
        # Take the newest N by descending id, then flip back to chronological.
        stmt = stmt.order_by(messages.c.id.desc()).limit(limit)
        with get_engine().connect() as conn:
            rows = list(reversed(conn.execute(stmt).fetchall()))
        # The window must open on a user message. Slicing "newest N" of a
        # strictly alternating history lands on an assistant message half the
        # time, and the API rejects a conversation that starts with one — which
        # broke every agent permanently once it passed the limit.
        while rows and rows[0].role != "user":
            rows.pop(0)
    return [
        {
            "role": r.role,
            "content": r.content,
            "created_at": r.created_at.isoformat() if r.created_at else None,
        }
        for r in rows
    ]


def clear_messages(agent_id: str) -> None:
    with get_engine().begin() as conn:
        conn.execute(delete(messages).where(messages.c.agent_id == agent_id))


# --- Jobs ------------------------------------------------------------------


def create_job(agent_id: str) -> str:
    job_id = str(uuid.uuid4())
    with get_engine().begin() as conn:
        conn.execute(
            insert(jobs).values(id=job_id, agent_id=agent_id, status="pending", created_at=_now())
        )
    return job_id


def update_job(job_id: str, **fields: Any) -> None:
    with get_engine().begin() as conn:
        conn.execute(update(jobs).where(jobs.c.id == job_id).values(**fields))


def get_job(job_id: str) -> dict[str, Any] | None:
    with get_engine().connect() as conn:
        row = conn.execute(select(jobs).where(jobs.c.id == job_id)).fetchone()
    if not row:
        return None
    return {
        "id": row.id,
        "agent_id": row.agent_id,
        "status": row.status,
        "reply": row.reply,
        "error": row.error,
        "pending_email": row.pending_email,
        "pending_token": row.pending_token,
        "conversation": row.conversation,
    }


def claim_confirmation(job_id: str, token: str) -> bool:
    """Atomically take ownership of a parked confirmation.

    A single conditional UPDATE is the whole guard. Read-then-write let two
    concurrent confirms both pass the status check and both send the email, and
    an in-process lock cannot help because the workers are separate processes.
    Matching on the token as well means a replayed confirm cannot approve a
    *different* message that parked in the meantime.

    Returns True exactly once per parked message.
    """
    with get_engine().begin() as conn:
        result = conn.execute(
            update(jobs)
            .where(
                jobs.c.id == job_id,
                jobs.c.status == "awaiting_confirmation",
                jobs.c.pending_token == token,
            )
            .values(status="pending", pending_token=None)
        )
        return result.rowcount == 1


# --- One-time migration ----------------------------------------------------


def _import_legacy_bots_json(path: str = "bots.json") -> None:
    """Bring agents from the old JSON file into the database, once.

    The previous version kept agents in bots.json on local disk. Importing on
    first boot means an existing install keeps its bots instead of appearing to
    have lost them.
    """
    if not os.path.exists(path):
        return
    # Guard on "have we imported before", not "is the table empty". Keying off
    # row count meant deleting every agent and restarting resurrected them all.
    if _migration_done(LEGACY_IMPORT_MARKER):
        return
    try:
        with open(path, encoding="utf-8") as fh:
            legacy = json.load(fh)
    except (OSError, json.JSONDecodeError, ValueError):
        _mark_migration_done(LEGACY_IMPORT_MARKER)
        return
    if isinstance(legacy, list):
        for entry in legacy[:MAX_LEGACY_ENTRIES]:
            if not isinstance(entry, dict):
                continue
            name = entry.get("name")
            if not isinstance(name, str) or not name.strip():
                continue
            try:
                create_agent(
                    name=name.strip()[:200],
                    agent_type=str(entry.get("type") or "bot")[:50],
                    instructions=str(entry.get("instructions") or "")[:100_000],
                )
            except Exception:
                # One malformed row must not take the process down. init_db()
                # runs at import time, so an exception here is a boot crash
                # loop across every worker rather than a skipped agent.
                continue
    _mark_migration_done(LEGACY_IMPORT_MARKER)
