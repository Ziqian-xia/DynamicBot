"""
SQLite-backed storage for DynamicBot studies.
Uses synchronous sqlite3 (stdlib) — admin operations are infrequent.
Database path: DATA_DIR env var (Railway volume) or ./backend/ for local dev.
"""

import json
import os
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# Allow overriding DB location via env var (e.g. Railway persistent volume at /data)
_DATA_DIR = Path(os.environ.get("DATA_DIR", Path(__file__).parent))
DB_PATH = _DATA_DIR / "dynamicbot.db"

# ---------------------------------------------------------------------------
# Default prompt content — pre-fills the form for new studies
# ---------------------------------------------------------------------------

DEFAULT_RESEARCH_CONTEXT = """You are a neutral research interviewer conducting a study on the Inflation Reduction Act (IRA) and clean energy manufacturing in the United States. Your role is to present specific local project information to respondents and gather their authentic perspectives through a structured conversation.

You must:
1. Present the project facts naturally and conversationally in your opening message — do not bullet-list every field mechanically.
2. Ask exactly one open-ended question per respondent turn.
3. Briefly acknowledge each response before asking the next question.
4. Never express personal opinions, make policy recommendations, or take sides.
5. Keep each response under 150 words.
6. Maintain a warm, professional, and genuinely curious tone throughout."""

DEFAULT_QUESTION_GUIDANCE = """Turn 1: Focus on awareness and initial reaction. Ask whether they were aware of this project and what their first thoughts are upon learning about it.

Turn 2: Explore community or economic impact. Ask about perceived benefits or concerns for the local community or workforce.

Turn 3 (FINAL TURN): Explore the personal values dimension. Ask how projects like this fit into their vision for the region's future. After their response, close gracefully — thank them warmly and note that their input will contribute to the research."""

# ---------------------------------------------------------------------------
# DB init
# ---------------------------------------------------------------------------


def init_db() -> None:
    _DATA_DIR.mkdir(parents=True, exist_ok=True)
    with _connect() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS studies (
                id                  TEXT PRIMARY KEY,
                name                TEXT NOT NULL,
                description         TEXT NOT NULL DEFAULT '',
                created_at          TEXT NOT NULL,
                updated_at          TEXT NOT NULL,
                projects_data       TEXT NOT NULL,
                research_context    TEXT NOT NULL,
                question_guidance   TEXT NOT NULL,
                max_turns           INTEGER NOT NULL DEFAULT 3,
                temperature         REAL NOT NULL DEFAULT 0.3,
                model               TEXT NOT NULL DEFAULT 'gpt-4o'
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS config (
                key   TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
        """)
        conn.commit()


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    return conn


def _row_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    d["projects_data"] = json.loads(d["projects_data"])
    return d


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ---------------------------------------------------------------------------
# Public CRUD
# ---------------------------------------------------------------------------


def list_studies() -> list[dict]:
    with _connect() as conn:
        rows = conn.execute(
            "SELECT id, name, description, created_at, updated_at, max_turns "
            "FROM studies ORDER BY created_at DESC"
        ).fetchall()
    return [dict(r) for r in rows]


def get_study(study_id: str) -> Optional[dict]:
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM studies WHERE id = ?", (study_id,)
        ).fetchone()
    return _row_to_dict(row) if row else None


def create_study(data: dict) -> dict:
    study_id = str(uuid.uuid4())
    now = _now()
    with _connect() as conn:
        conn.execute(
            """INSERT INTO studies
               (id, name, description, created_at, updated_at,
                projects_data, research_context, question_guidance,
                max_turns, temperature, model)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                study_id,
                data["name"],
                data.get("description", ""),
                now, now,
                json.dumps(data["projects_data"]),
                data["research_context"],
                data["question_guidance"],
                int(data.get("max_turns", 3)),
                float(data.get("temperature", 0.3)),
                data.get("model", "gpt-4o"),
            ),
        )
        conn.commit()
    return get_study(study_id)


def update_study(study_id: str, data: dict) -> Optional[dict]:
    with _connect() as conn:
        result = conn.execute(
            """UPDATE studies
               SET name=?, description=?, updated_at=?,
                   projects_data=?, research_context=?, question_guidance=?,
                   max_turns=?, temperature=?, model=?
               WHERE id=?""",
            (
                data["name"],
                data.get("description", ""),
                _now(),
                json.dumps(data["projects_data"]),
                data["research_context"],
                data["question_guidance"],
                int(data.get("max_turns", 3)),
                float(data.get("temperature", 0.3)),
                data.get("model", "gpt-4o"),
                study_id,
            ),
        )
        conn.commit()
    return get_study(study_id) if result.rowcount else None


def delete_study(study_id: str) -> bool:
    with _connect() as conn:
        result = conn.execute("DELETE FROM studies WHERE id = ?", (study_id,))
        conn.commit()
    return result.rowcount > 0


def get_config(key: str) -> Optional[str]:
    with _connect() as conn:
        row = conn.execute("SELECT value FROM config WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_config(key: str, value: str) -> None:
    with _connect() as conn:
        conn.execute(
            "INSERT INTO config (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        conn.commit()


def count_tracts(study_id: str) -> int:
    """Return the number of Census Tract entries (excluding DEFAULT) in a study."""
    study = get_study(study_id)
    if not study:
        return 0
    return sum(1 for k in study["projects_data"] if k != "DEFAULT")
