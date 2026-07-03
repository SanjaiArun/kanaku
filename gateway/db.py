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
        # Check the target profile actually exists first: the UPDATE
        # below always touches every row for this user regardless of
        # whether profile_name matched anything (it sets is_active to
        # the boolean result of the comparison, not a conditional
        # WHERE), so without this check a bad name would silently
        # report success while leaving zero profiles active.
        cur.execute(
            "SELECT 1 FROM user_profiles WHERE telegram_user_id=%s AND profile_name=%s",
            (telegram_user_id, profile_name),
        )
        if cur.fetchone() is None:
            return False
        # Two statements, not one UPDATE ... SET is_active=(profile_name=%s):
        # Postgres doesn't guarantee row-by-row constraint checking order
        # within a single multi-row UPDATE against a non-deferrable unique
        # index, so setting one row to TRUE and another to FALSE in the
        # same statement can transiently violate one_active_profile_per_user
        # if it happens to activate the new row before deactivating the old
        # one — confirmed in practice once a user had 2 profile rows for
        # the first time. Deactivating everyone else first, then activating
        # the target, keeps the active-row count monotonically <=1 at every
        # step.
        cur.execute(
            "UPDATE user_profiles SET is_active=FALSE WHERE telegram_user_id=%s AND profile_name != %s",
            (telegram_user_id, profile_name),
        )
        cur.execute(
            "UPDATE user_profiles SET is_active=TRUE WHERE telegram_user_id=%s AND profile_name=%s",
            (telegram_user_id, profile_name),
        )
        conn.commit()
    return True


def count_profiles(telegram_user_id) -> int:
    with db_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM user_profiles WHERE telegram_user_id=%s", (telegram_user_id,))
        return cur.fetchone()[0]


def profile_exists(telegram_user_id, profile_name) -> bool:
    with db_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM user_profiles WHERE telegram_user_id=%s AND profile_name=%s",
            (telegram_user_id, profile_name),
        )
        return cur.fetchone() is not None


def create_profile_row(telegram_user_id, profile_name, firefly_pat, firefly_base_url) -> None:
    with db_conn() as conn, conn.cursor() as cur:
        cur.execute(
            "UPDATE user_profiles SET is_active=FALSE WHERE telegram_user_id=%s",
            (telegram_user_id,),
        )
        # ON CONFLICT: LangGraph can retry a tool call that already
        # partially succeeded (observed in practice — a transient error
        # elsewhere in the same turn causes the pending tools-node task
        # to be silently re-run on the next graph.invoke()), so a
        # second call with the same (telegram_user_id, profile_name)
        # must not crash — just reactivate/refresh it instead of a
        # plain INSERT that would violate the active-profile or
        # profile_name uniqueness constraints.
        cur.execute(
            """
            INSERT INTO user_profiles (telegram_user_id, profile_name, firefly_pat, firefly_base_url, is_active)
            VALUES (%s, %s, %s, %s, TRUE)
            ON CONFLICT (telegram_user_id, profile_name)
            DO UPDATE SET firefly_pat=EXCLUDED.firefly_pat,
                           firefly_base_url=EXCLUDED.firefly_base_url,
                           is_active=TRUE
            """,
            (telegram_user_id, profile_name, firefly_pat, firefly_base_url),
        )
        conn.commit()


def get_tg_offset() -> int:
    with db_conn() as conn, conn.cursor() as cur:
        cur.execute("SELECT last_update_id FROM telegram_offset WHERE id=1")
        row = cur.fetchone()
    return row[0] if row else 0


def set_tg_offset(offset: int) -> None:
    with db_conn() as conn, conn.cursor() as cur:
        cur.execute("UPDATE telegram_offset SET last_update_id=%s WHERE id=1", (offset,))
        conn.commit()
