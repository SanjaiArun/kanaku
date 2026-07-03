"""Provisions brand-new, isolated Firefly III user accounts for new
Telegram users — no manual setup step per user.

Firefly's public API can create a user (`POST /api/v1/users`) but does
NOT let you set a working login password through it — confirmed
empirically: the password value Firefly stores after that call isn't a
valid bcrypt hash, so logging in with the password you sent always
fails ("credentials do not match"). And Firefly has no API at all for
minting a Personal Access Token — that's only exposed via a
session-authenticated web route Firefly's own UI calls.

To work around both: patch a properly-hashed password directly into
Firefly's own `users` table (we already have Postgres access to it in
this docker-compose stack, since it's the same Postgres instance as
our own `kanakku` database), then drive the same login -> CSRF ->
token-mint flow Firefly's web UI uses. Verified this produces a token
that authenticates identically to a UI-created PAT.

This is inherently coupled to Firefly's internal routes and `users`
table schema rather than its documented public API, so it's the single
most fragile part of this codebase with respect to a future Firefly
version upgrade — kept isolated in this module so a break is easy to
find and fix.
"""
import os
import re
import secrets
import string

import bcrypt
import psycopg
import requests

FIREFLY_BASE_URL = os.environ.get("FIREFLY_BASE_URL", "http://firefly_iii:8080")
FIREFLY_ADMIN_PAT = os.environ["FIREFLY_ADMIN_PAT"]

DB_HOST = os.environ["DB_HOST"]
DB_USER = os.environ["DB_USER"]
DB_PASSWORD = os.environ["DB_PASSWORD"]


def _firefly_db_conn():
    # Deliberately dbname="firefly", not the gateway's own DB_NAME —
    # this connects to Firefly's own database in the same Postgres
    # instance (see db/1-init-multi-db.sh).
    return psycopg.connect(f"host={DB_HOST} dbname=firefly user={DB_USER} password={DB_PASSWORD}")


def _admin_headers():
    return {
        "Authorization": f"Bearer {FIREFLY_ADMIN_PAT}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


def _generate_password() -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(24))


def _create_firefly_user(email: str, password: str) -> None:
    r = requests.post(
        f"{FIREFLY_BASE_URL}/api/v1/users",
        headers=_admin_headers(),
        json={"email": email, "password": password, "password_confirmation": password},
        timeout=10,
    )
    r.raise_for_status()


def _fix_password(email: str, password: str) -> None:
    hashed = bcrypt.hashpw(password.encode(), bcrypt.gensalt(rounds=10)).decode()
    with _firefly_db_conn() as conn, conn.cursor() as cur:
        cur.execute("UPDATE users SET password=%s WHERE email=%s", (hashed, email))
        if cur.rowcount != 1:
            raise RuntimeError(f"expected to update exactly 1 Firefly user row for {email}, updated {cur.rowcount}")
        conn.commit()


def _extract_csrf(html: str) -> str:
    match = re.search(r'name="csrf-token" content="([^"]+)"', html) or \
        re.search(r'name="_token" value="([^"]+)"', html)
    if not match:
        raise RuntimeError("could not find a CSRF token on Firefly's login/oauth page")
    return match.group(1)


def _mint_pat(email: str, password: str, token_name: str) -> str:
    session = requests.Session()

    login_page = session.get(f"{FIREFLY_BASE_URL}/login", timeout=10)
    login_page.raise_for_status()
    csrf = _extract_csrf(login_page.text)

    resp = session.post(
        f"{FIREFLY_BASE_URL}/login",
        data={"_token": csrf, "email": email, "password": password},
        timeout=10,
    )
    if resp.url.rstrip("/").endswith("/login"):
        raise RuntimeError(f"login failed for provisioned Firefly user {email} (redirected back to /login)")

    oauth_page = session.get(f"{FIREFLY_BASE_URL}/profile/oauth", timeout=10)
    oauth_page.raise_for_status()
    csrf2 = _extract_csrf(oauth_page.text)

    token_resp = session.post(
        f"{FIREFLY_BASE_URL}/oauth/personal-access-tokens",
        json={"name": token_name, "scopes": []},
        headers={"Accept": "application/json", "X-CSRF-TOKEN": csrf2},
        timeout=10,
    )
    token_resp.raise_for_status()
    return token_resp.json()["accessToken"]


def provision_firefly_account(telegram_user_id: int, profile_name: str) -> dict:
    """Creates a brand-new, isolated Firefly III user + real Personal
    Access Token for one profile. Returns {"firefly_pat", "firefly_base_url"}
    ready to insert into user_profiles."""
    email = f"tg{telegram_user_id}-{profile_name}@users.kanakku.local"
    password = _generate_password()
    _create_firefly_user(email, password)
    _fix_password(email, password)
    pat = _mint_pat(email, password, token_name=f"kanakku-{profile_name}")
    return {"firefly_pat": pat, "firefly_base_url": FIREFLY_BASE_URL}
