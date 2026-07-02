"""Postgres helpers for user profiles and the Telegram polling offset.

Conversation memory (clarifications, pending confirmations, chat history)
used to live in a hand-rolled `conversation_state` table. That table is
gone: the LangGraph agent's Postgres checkpointer (see agent.py) now owns
all per-user conversation state.
"""
import os

import psycopg
from psycopg.rows import dict_row

DB_HOST = os.environ["DB_HOST"]
DB_NAME = os.environ["DB_NAME"]
DB_USER = os.environ["DB_USER"]
DB_PASSWORD = os.environ["DB_PASSWORD"]


def conn_string() -> str:
    return f"host={DB_HOST} dbname={DB_NAME} user={DB_USER} password={DB_PASSWORD}"


def db_conn():
    return psycopg.connect(conn_string())


def get_active_profile(telegram_user_id):
    with db_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT * FROM user_profiles WHERE telegram_user_id=%s AND is_active=TRUE",
            (telegram_user_id,),
        )
        row = cur.fetchone()
    return dict(row) if row else None


def get_all_profiles(telegram_user_id):
    with db_conn() as conn, conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT * FROM user_profiles WHERE telegram_user_id=%s ORDER BY profile_name",
            (telegram_user_id,),
        )
        rows = cur.fetchall()
    return [dict(r) for r in rows]


def switch_profile(telegram_user_id, profile_name) -> bool:
    with db_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE user_profiles SET is_active=(profile_name=%s) WHERE telegram_user_id=%s",
            (profile_name, telegram_user_id),
        )
        affected = cur.rowcount
        conn.commit()
    return affected > 0


def get_tg_offset() -> int:
    with db_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT last_update_id FROM telegram_offset WHERE id=1")
        row = cur.fetchone()
    return row[0] if row else 0


def set_tg_offset(offset: int) -> None:
    with db_conn() as conn, conn.cursor() as cur:
        cur.execute("UPDATE telegram_offset SET last_update_id=%s WHERE id=1", (offset,))
        conn.commit()
