"""
Kanakku Gateway
===============
Direct Telegram polling — no n8n required for message intake.
Handles natural language expense/income/transfer logging with
clarification flows, account resolution, and full command support.
Queues messages for retry when Ollama or Firefly III are temporarily down.
"""

import os, time, json, logging, difflib, datetime
import psycopg2, psycopg2.extras, requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("kanakku")

# ── Config ────────────────────────────────────────────────────────────────────
DB_HOST      = os.environ["DB_HOST"]
DB_NAME      = os.environ["DB_NAME"]
DB_USER      = os.environ["DB_USER"]
DB_PASSWORD  = os.environ["DB_PASSWORD"]
FIREFLY_URL  = os.environ["FIREFLY_URL"]
TG_TOKEN     = os.environ["TELEGRAM_BOT_TOKEN"]
GROQ_API_KEY = os.environ["GROQ_API_KEY"]
GROQ_MODEL   = os.environ.get("GROQ_MODEL", "llama-3.1-8b-instant")
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL_SECONDS", "30"))
MAX_RETRIES  = 5
TG_API       = f"https://api.telegram.org/bot{TG_TOKEN}"
GROQ_API     = "https://api.groq.com/openai/v1/chat/completions"

# ── DB helpers ────────────────────────────────────────────────────────────────
def db_conn():
    return psycopg2.connect(host=DB_HOST, dbname=DB_NAME, user=DB_USER, password=DB_PASSWORD)

def get_active_profile(telegram_user_id):
    conn = db_conn()
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("SELECT * FROM user_profiles WHERE telegram_user_id=%s AND is_active=TRUE", (telegram_user_id,))
        row = cur.fetchone()
    conn.close()
    return dict(row) if row else None

def get_all_profiles(telegram_user_id):
    conn = db_conn()
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("SELECT * FROM user_profiles WHERE telegram_user_id=%s ORDER BY profile_name", (telegram_user_id,))
        rows = cur.fetchall()
    conn.close()
    return [dict(r) for r in rows]

def switch_profile(telegram_user_id, profile_name):
    conn = db_conn()
    conn.autocommit = False
    try:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE user_profiles SET is_active=(profile_name=%s) WHERE telegram_user_id=%s",
                (profile_name, telegram_user_id)
            )
            affected = cur.rowcount
        conn.commit()
        return affected > 0
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

def get_conversation_state(telegram_user_id):
    conn = db_conn()
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute("SELECT state, context FROM conversation_state WHERE telegram_user_id=%s", (telegram_user_id,))
        row = cur.fetchone()
    conn.close()
    return (row["state"], row["context"] or {}) if row else ("idle", {})

def set_conversation_state(telegram_user_id, state, context):
    conn = db_conn()
    with conn.cursor() as cur:
        cur.execute("""
            INSERT INTO conversation_state (telegram_user_id, state, context, updated_at)
            VALUES (%s, %s, %s, now())
            ON CONFLICT (telegram_user_id) DO UPDATE
              SET state=EXCLUDED.state, context=EXCLUDED.context, updated_at=now()
        """, (telegram_user_id, state, json.dumps(context)))
    conn.commit()
    conn.close()

def clear_conversation_state(telegram_user_id):
    set_conversation_state(telegram_user_id, "idle", {})

def get_tg_offset():
    conn = db_conn()
    with conn.cursor() as cur:
        cur.execute("SELECT last_update_id FROM telegram_offset WHERE id=1")
        row = cur.fetchone()
    conn.close()
    return row[0] if row else 0

def set_tg_offset(offset):
    conn = db_conn()
    with conn.cursor() as cur:
        cur.execute("UPDATE telegram_offset SET last_update_id=%s WHERE id=1", (offset,))
    conn.commit()
    conn.close()

def queue_message(telegram_user_id, profile_id, update_id, raw_text):
    conn = db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO messages_queue (telegram_user_id, profile_id, telegram_update_id, raw_text)
                VALUES (%s, %s, %s, %s) ON CONFLICT (telegram_update_id) DO NOTHING
            """, (telegram_user_id, profile_id, update_id, raw_text))
        conn.commit()
    finally:
        conn.close()

def bump_retry(message_id, next_status, error_text):
    conn = db_conn()
    with conn.cursor() as cur:
        cur.execute("""
            UPDATE messages_queue
            SET retry_count=retry_count+1,
                status=CASE WHEN retry_count+1 >= %s THEN 'failed' ELSE %s END,
                last_error=%s, updated_at=now()
            WHERE id=%s
        """, (MAX_RETRIES, next_status, error_text, message_id))
    conn.commit()
    conn.close()

# ── Telegram ──────────────────────────────────────────────────────────────────
def tg_get_updates(offset):
    try:
        r = requests.get(f"{TG_API}/getUpdates", params={"offset": offset, "timeout": 20}, timeout=25)
        r.raise_for_status()
        return r.json().get("result", [])
    except requests.RequestException as e:
        log.warning("getUpdates failed: %s", e)
        return []

def tg_send(chat_id, text):
    try:
        requests.post(f"{TG_API}/sendMessage", json={"chat_id": chat_id, "text": text}, timeout=10)
    except requests.RequestException as e:
        log.warning("sendMessage failed to %s: %s", chat_id, e)

# ── Firefly III API ───────────────────────────────────────────────────────────
def ff_headers(pat):
    return {"Authorization": f"Bearer {pat}", "Accept": "application/json", "Content-Type": "application/json"}

def ff_get_accounts(profile, account_type=None):
    params = {"limit": 200}
    if account_type:
        params["type"] = account_type
    r = requests.get(f"{profile['firefly_base_url']}/api/v1/accounts",
                     headers=ff_headers(profile["firefly_pat"]), params=params, timeout=10)
    r.raise_for_status()
    return r.json().get("data", [])

def ff_create_account(profile, name, account_type, account_role=None):
    payload = {"name": name, "type": account_type,
               "opening_balance": "0", "opening_balance_date": datetime.date.today().isoformat()}
    if account_role:
        payload["account_role"] = account_role
    r = requests.post(f"{profile['firefly_base_url']}/api/v1/accounts",
                      headers=ff_headers(profile["firefly_pat"]), json=payload, timeout=10)
    r.raise_for_status()
    return r.json()["data"]

def ff_get_categories(profile):
    r = requests.get(f"{profile['firefly_base_url']}/api/v1/categories",
                     headers=ff_headers(profile["firefly_pat"]), params={"limit": 200}, timeout=10)
    r.raise_for_status()
    return r.json().get("data", [])

def ff_post_transaction(profile, payload):
    r = requests.post(f"{profile['firefly_base_url']}/api/v1/transactions",
                      headers=ff_headers(profile["firefly_pat"]), json=payload, timeout=15)
    r.raise_for_status()
    return r.json()["data"]

def ff_get_recent(profile, limit=5):
    r = requests.get(f"{profile['firefly_base_url']}/api/v1/transactions",
                     headers=ff_headers(profile["firefly_pat"]),
                     params={"limit": limit, "type": "default"}, timeout=10)
    r.raise_for_status()
    return r.json().get("data", [])

def ff_delete_transaction(profile, transaction_id):
    r = requests.delete(f"{profile['firefly_base_url']}/api/v1/transactions/{transaction_id}",
                        headers=ff_headers(profile["firefly_pat"]), timeout=10)
    r.raise_for_status()

def ff_update_transaction(profile, transaction_id, payload):
    r = requests.put(f"{profile['firefly_base_url']}/api/v1/transactions/{transaction_id}",
                     headers=ff_headers(profile["firefly_pat"]), json=payload, timeout=15)
    r.raise_for_status()
    return r.json()["data"]

def ff_get_monthly_summary(profile):
    today = datetime.date.today()
    start = today.replace(day=1).isoformat()
    r = requests.get(f"{profile['firefly_base_url']}/api/v1/insight/expense/category",
                     headers=ff_headers(profile["firefly_pat"]),
                     params={"start": start, "end": today.isoformat()}, timeout=10)
    r.raise_for_status()
    return r.json()

def ff_get_budgets(profile):
    r = requests.get(f"{profile['firefly_base_url']}/api/v1/budgets",
                     headers=ff_headers(profile["firefly_pat"]), params={"limit": 50}, timeout=10)
    r.raise_for_status()
    return r.json().get("data", [])

def ff_search_transactions(profile, query):
    r = requests.get(f"{profile['firefly_base_url']}/api/v1/search/transactions",
                     headers=ff_headers(profile["firefly_pat"]),
                     params={"query": query, "limit": 5}, timeout=10)
    r.raise_for_status()
    return r.json().get("data", [])

# ── Service health ────────────────────────────────────────────────────────────
def service_up(url):
    try:
        return requests.get(url, timeout=3).status_code < 500
    except requests.RequestException:
        return False

def groq_healthy():
    try:
        r = requests.get("https://api.groq.com/openai/v1/models",
                         headers={"Authorization": f"Bearer {GROQ_API_KEY}"}, timeout=5)
        return r.status_code == 200
    except requests.RequestException:
        return False

def firefly_healthy(base_url):
    return service_up(f"{base_url}/health")

# ── Fuzzy account matching ────────────────────────────────────────────────────
def get_account_names(profile, account_type=None):
    return [a["attributes"]["name"] for a in ff_get_accounts(profile, account_type)]

def fuzzy_match(name, candidates):
    if not candidates:
        return None, 0.0
    name_lower = name.lower()
    best, best_score = None, 0.0
    for c in candidates:
        score = difflib.SequenceMatcher(None, name_lower, c.lower()).ratio()
        if score > best_score:
            best, best_score = c, score
    return best, best_score

def resolve_account(profile, name, allowed_types):
    """Returns (resolved_name, action): action is 'found' | 'ambiguous' | 'not_found'"""
    if not name:
        return None, "not_found"
    candidates = []
    for t in allowed_types:
        candidates.extend(get_account_names(profile, t))
    for c in candidates:
        if c.lower() == name.lower():
            return c, "found"
    best, score = fuzzy_match(name, candidates)
    if score >= 0.8:
        return best, "found"
    if score >= 0.5:
        return best, "ambiguous"
    return name, "not_found"

# ── LLM ──────────────────────────────────────────────────────────────────────
PARSE_SYSTEM = (
    "You extract financial transaction data from a message and reply with ONLY a JSON object.\n\n"
    "Schema:\n"
    '{"type":"withdrawal|deposit|transfer|unknown","amount":<number|null>,'
    '"source":"<account money comes FROM or null>","destination":"<account money goes TO or null>",'
    '"category":"<category or null>","description":"<brief note or null>",'
    '"date":"<YYYY-MM-DD, use today if not mentioned>",'
    '"reply_language":"english|tamil|hindi",'
    '"needs_clarification":<true|false>,'
    '"missing_fields":["amount","type",...],'
    '"question":"<ask in reply_language if needs_clarification, else empty string>"}\n\n'
    "Rules:\n"
    "- withdrawal: spending (spent, paid, bought, கொடுத்தேன், खर्च)\n"
    "- deposit: receiving money (received, got, salary, வந்தது, मिला)\n"
    "- transfer: moving between own accounts\n"
    "- withdrawal: source=payment account (SBI, GPay, cash), destination=what it was spent on\n"
    "- deposit: source=where money came from (salary, client), destination=receiving account\n"
    "- transfer: both source and destination are required\n"
    "- REQUIRED fields: amount, type, source (for withdrawal)\n"
    "- If amount is missing → ask 'How much was it?'\n"
    "- If type is unclear → ask 'Was this an expense, income, or transfer?'\n"
    "- If source (payment method) is missing for withdrawal → ask 'Which account or payment method did you use? (e.g. SBI, GPay, cash)'\n"
    "- source = the payment method/bank account used (SBI, HDFC, GPay, cash, UPI)\n"
    "- destination = what the money was spent on (shop name, person, merchant) — optional, can be null\n"
    "- DO NOT ask about destination, category, or description — these are optional\n"
    "- DO NOT guess or assume source — if not mentioned, ask\n"
    "- ask only ONE question at a time\n"
    "- question must be in the same language as reply_language\n"
    "- category: Food, Transport, Rent, Shopping, Medical, Entertainment, Salary, Other\n"
    f"- today: {datetime.date.today().isoformat()}"
)

ACCOUNT_TYPE_SYSTEM = (
    "Identify the Firefly III account type from the user's description. "
    "Reply with ONLY a JSON object:\n"
    '{"account_name":"<clean name>","firefly_type":"asset|expense|revenue|liability",'
    '"account_role":"defaultAsset|savingAsset|ccAsset|cashWalletAsset|null",'
    '"clear_enough":<true|false>,"question":"<ask if unclear, else empty string>"}\n\n'
    "Rules:\n"
    "- savings/bank/current account → asset, savingAsset\n"
    "- credit card → asset, ccAsset\n"
    "- wallet/cash/GPay/UPI/PhonePe → asset, cashWalletAsset\n"
    "- shop/restaurant/merchant/expense → expense, null\n"
    "- salary/income/freelance/client/revenue → revenue, null\n"
    "- loan/mortgage → liability, null\n"
    "- if unclear between types, set clear_enough=false and ask in question field"
)

def groq_call(system, prompt):
    r = requests.post(GROQ_API, headers={
        "Authorization": f"Bearer {GROQ_API_KEY}",
        "Content-Type": "application/json",
    }, json={
        "model": GROQ_MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user",   "content": prompt},
        ],
        "response_format": {"type": "json_object"},
        "temperature": 0,
    }, timeout=30)
    r.raise_for_status()
    return json.loads(r.json()["choices"][0]["message"]["content"])

def parse_transaction(text):
    return groq_call(PARSE_SYSTEM, text)

def parse_account_type(text):
    return groq_call(ACCOUNT_TYPE_SYSTEM, text)

# ── Format helpers ────────────────────────────────────────────────────────────
def txn_attrs(t):
    a = t.get("attributes", {})
    splits = a.get("transactions")
    return splits[0] if splits else a

def fmt_transaction(t):
    a = txn_attrs(t)
    return (f"₹{a.get('amount','?')} {a.get('type','?')} — "
            f"{a.get('description') or a.get('category_name') or '?'} "
            f"({a.get('source_name','?')} → {a.get('destination_name','?')}) "
            f"on {str(a.get('date','?'))[:10]}")

def receipt(parsed, profile_name):
    emoji = {"withdrawal": "💸", "deposit": "💰", "transfer": "🔄"}.get(parsed.get("type", ""), "📝")
    parts = [f"{emoji} [{profile_name}] ₹{parsed['amount']} {parsed.get('type','')}"]
    if parsed.get("source") or parsed.get("destination"):
        parts.append(f"{parsed.get('source','?')} → {parsed.get('destination','?')}")
    if parsed.get("category"):
        parts.append(f"Category: {parsed['category']}")
    if parsed.get("description"):
        parts.append(f"Note: {parsed['description']}")
    return "\n".join(parts)

# ── Command handlers ──────────────────────────────────────────────────────────
def cmd_start(chat_id):
    tg_send(chat_id,
        "Welcome to Kanakku!\n\n"
        "Kanakku (கணக்கு) is your personal finance assistant. "
        "Just send a message in plain English, Tamil, or Hindi "
        "and it logs your expenses, income, and transfers "
        "directly into your private Firefly III ledger.\n\n"
        "Examples:\n"
        "  spent 500 on food using SBI\n"
        "  got salary 50000 in HDFC\n"
        "  transferred 2000 from SBI to HDFC\n\n"
        "Commands:\n"
        "  /balance    — account balances\n"
        "  /recent     — last 5 transactions\n"
        "  /summary    — this month by category\n"
        "  /budget     — budget limits & spending\n"
        "  /edit       — edit last transaction\n"
        "  /undo       — delete last transaction\n"
        "  /find       — search transactions\n"
        "  /accounts   — list all accounts\n"
        "  /categories — list categories\n"
        "  /newaccount — create an account\n"
        "  /switch     — switch profile\n"
        "  /help       — show all commands"
    )

def cmd_help(chat_id):
    tg_send(chat_id,
        "Kanakku commands:\n\n"
        "/balance — account balances\n"
        "/accounts — list all accounts\n"
        "/categories — list categories\n"
        "/newaccount <name type> — create account\n"
        "/recent — last 5 transactions\n"
        "/undo — delete last transaction\n"
        "/summary — this month by category\n"
        "/budget — budget limits & spending\n"
        "/find <keyword> — search transactions\n"
        "/switch <profile> — switch active profile\n"
        "/edit — edit last transaction\n\n"
        "Or just send a message:\n"
        "  spent 500 on food using SBI\n"
        "  got salary 50000 in HDFC\n"
        "  transferred 2000 from SBI to HDFC"
    )

def cmd_balance(chat_id, profile):
    accounts = ff_get_accounts(profile, "asset")
    if not accounts:
        tg_send(chat_id, "No asset accounts found.")
        return
    lines = [f"{a['attributes']['name']}: ₹{a['attributes'].get('current_balance','?')}" for a in accounts]
    tg_send(chat_id, "Balances:\n" + "\n".join(lines))

def cmd_accounts(chat_id, profile):
    accounts = ff_get_accounts(profile)
    if not accounts:
        tg_send(chat_id, "No accounts found.")
        return
    by_type = {}
    for a in accounts:
        t = a["attributes"].get("type", "other")
        by_type.setdefault(t, []).append(a["attributes"]["name"])
    lines = []
    for t, names in sorted(by_type.items()):
        lines.append(f"\n{t.upper()}:")
        lines.extend(f"  • {n}" for n in names)
    tg_send(chat_id, "Accounts:" + "".join(lines))

def cmd_categories(chat_id, profile):
    cats = ff_get_categories(profile)
    if not cats:
        tg_send(chat_id, "No categories yet.")
        return
    names = [c["attributes"]["name"] for c in cats]
    tg_send(chat_id, "Categories:\n" + "\n".join(f"• {n}" for n in names))

def cmd_newaccount(chat_id, profile, args, telegram_user_id):
    if not args:
        tg_send(chat_id,
            "Usage: /newaccount <name and type>\nExamples:\n"
            "  /newaccount SBI savings account\n"
            "  /newaccount HDFC credit card\n"
            "  /newaccount Swiggy expense"
        )
        return
    try:
        parsed = parse_account_type(args)
    except Exception:
        tg_send(chat_id, "Couldn't understand that. Try: /newaccount SBI savings account")
        return
    if not parsed.get("clear_enough"):
        tg_send(chat_id, parsed.get("question") or "Could you be more specific about the account type?")
        set_conversation_state(telegram_user_id, "newaccount_clarify", {"args": args})
        return
    _create_account_and_notify(chat_id, profile, parsed)

def _create_account_and_notify(chat_id, profile, parsed):
    role = parsed.get("account_role")
    if role == "null":
        role = None
    try:
        ff_create_account(profile, parsed["account_name"], parsed["firefly_type"], role)
        tg_send(chat_id, f"✅ Created '{parsed['account_name']}' ({parsed['firefly_type']})")
    except Exception as e:
        tg_send(chat_id, f"Failed to create account: {e}")

def cmd_recent(chat_id, profile):
    txns = ff_get_recent(profile, 5)
    if not txns:
        tg_send(chat_id, "No recent transactions.")
        return
    lines = [f"{i+1}. {fmt_transaction(t)}" for i, t in enumerate(txns)]
    tg_send(chat_id, "Recent transactions:\n" + "\n".join(lines))

def cmd_undo(chat_id, profile, telegram_user_id):
    txns = ff_get_recent(profile, 1)
    if not txns:
        tg_send(chat_id, "No transactions to undo.")
        return
    t = txns[0]
    summary = fmt_transaction(t)
    tg_send(chat_id, f"Delete this transaction?\n{summary}\n\nReply 'yes' to confirm.")
    set_conversation_state(telegram_user_id, "undo_confirm", {"transaction_id": t["id"], "summary": summary})

def cmd_summary(chat_id, profile):
    try:
        data = ff_get_monthly_summary(profile)
    except Exception as e:
        tg_send(chat_id, f"Could not fetch summary: {e}")
        return
    if not data:
        tg_send(chat_id, "No spending data this month.")
        return
    total = 0.0
    lines = []
    for item in data:
        name = item.get("name", "?")
        amount = abs(float(item.get("difference_float", 0)))
        total += amount
        lines.append(f"• {name}: ₹{amount:.0f}")
    lines.append(f"\nTotal: ₹{total:.0f}")
    month = datetime.date.today().strftime("%B %Y")
    tg_send(chat_id, f"Summary — {month}:\n" + "\n".join(lines))

def cmd_budget(chat_id, profile):
    try:
        budgets = ff_get_budgets(profile)
    except Exception as e:
        tg_send(chat_id, f"Could not fetch budgets: {e}")
        return
    if not budgets:
        tg_send(chat_id, "No budgets set up in Firefly III.\nCreate budgets at your Firefly III dashboard.")
        return
    lines = []
    for b in budgets:
        attrs = b["attributes"]
        name = attrs["name"]
        spent = abs(float((attrs.get("spent") or [{}])[0].get("sum", 0)))
        limit_val = float(attrs["auto_budget_amount"]) if attrs.get("auto_budget_amount") else None
        if limit_val:
            remaining = limit_val - spent
            lines.append(f"• {name}: ₹{spent:.0f} / ₹{limit_val:.0f} (₹{remaining:.0f} left)")
        else:
            lines.append(f"• {name}: ₹{spent:.0f} spent (no limit set)")
    tg_send(chat_id, "Budgets:\n" + "\n".join(lines))

def cmd_find(chat_id, profile, args):
    if not args:
        tg_send(chat_id, "Usage: /find <keyword>")
        return
    try:
        txns = ff_search_transactions(profile, args)
    except Exception as e:
        tg_send(chat_id, f"Search failed: {e}")
        return
    if not txns:
        tg_send(chat_id, f"No transactions found for '{args}'.")
        return
    lines = [f"{i+1}. {fmt_transaction(t)}" for i, t in enumerate(txns[:5])]
    tg_send(chat_id, f"Results for '{args}':\n" + "\n".join(lines))

def cmd_switch(chat_id, telegram_user_id, args):
    if not args:
        profiles = get_all_profiles(telegram_user_id)
        if not profiles:
            tg_send(chat_id, "No profiles found.")
            return
        lines = [f"{'✅' if p['is_active'] else '  '} {p['profile_name']}" for p in profiles]
        tg_send(chat_id, "Profiles:\n" + "\n".join(lines) + "\n\nUse /switch <name> to switch.")
        return
    if switch_profile(telegram_user_id, args.strip()):
        tg_send(chat_id, f"✅ Switched to '{args.strip()}'.")
    else:
        tg_send(chat_id, f"Profile '{args.strip()}' not found.")

# Edit field map: reply number → (firefly field name, input type)
EDIT_FIELDS = {
    "1": ("amount",           "free"),
    "2": ("source_name",      "account_asset"),
    "3": ("destination_name", "account_all"),
    "4": ("category_name",    "category"),
    "5": ("description",      "free"),
    "6": ("date",             "free"),
    "7": ("type",             "select_type"),
}

def cmd_edit(chat_id, profile, telegram_user_id):
    txns = ff_get_recent(profile, 1)
    if not txns:
        tg_send(chat_id, "No recent transactions to edit.")
        return
    t = txns[0]
    attrs = txn_attrs(t)
    tg_send(chat_id,
        f"Last transaction:\n{fmt_transaction(t)}\n\n"
        "What do you want to change?\n"
        "1. Amount\n"
        "2. Source account\n"
        "3. Destination account\n"
        "4. Category\n"
        "5. Description\n"
        "6. Date\n"
        "7. Type (withdrawal/deposit/transfer)"
    )
    set_conversation_state(telegram_user_id, "edit_pick_field", {
        "transaction_id": t["id"],
        "transaction": dict(attrs),
    })

# ── Conversation state handler ────────────────────────────────────────────────
def handle_conversation_reply(chat_id, telegram_user_id, profile, text):
    state, ctx = get_conversation_state(telegram_user_id)

    if state == "undo_confirm":
        if text.strip().lower() in ("yes", "y", "ha", "ஆம்", "हां"):
            try:
                ff_delete_transaction(profile, ctx["transaction_id"])
                tg_send(chat_id, "✅ Transaction deleted.")
            except Exception as e:
                tg_send(chat_id, f"Failed to delete: {e}")
        else:
            tg_send(chat_id, "Cancelled.")
        clear_conversation_state(telegram_user_id)
        return True

    if state == "clarify_transaction":
        combined = f"Original: {ctx.get('original_text','')} | Update: {text}"
        try:
            partial = ctx.get("parsed", {})
            update = parse_transaction(combined)
            for f in ("amount", "type", "source", "destination", "category", "description"):
                if update.get(f):
                    partial[f] = update[f]
            if update.get("needs_clarification") and update.get("question"):
                tg_send(chat_id, update["question"])
                set_conversation_state(telegram_user_id, "clarify_transaction",
                                       {**ctx, "parsed": partial, "original_text": combined})
                return True
        except Exception:
            tg_send(chat_id, "Sorry, I couldn't understand that. Please try again from the beginning.")
            clear_conversation_state(telegram_user_id)
            return True
        clear_conversation_state(telegram_user_id)
        attempt_post(chat_id, telegram_user_id, profile, partial, combined)
        return True

    if state == "account_type_needed":
        try:
            parsed_type = parse_account_type(f"{ctx['account_name']} {text}")
        except Exception:
            tg_send(chat_id, "Couldn't understand that. Please describe the account type more clearly.")
            return True
        if not parsed_type.get("clear_enough"):
            tg_send(chat_id, parsed_type.get("question") or "Please be more specific about the account type.")
            return True
        role = parsed_type.get("account_role")
        if role == "null":
            role = None
        try:
            ff_create_account(profile, ctx["account_name"], parsed_type["firefly_type"], role)
            tg_send(chat_id, f"✅ Created '{ctx['account_name']}' ({parsed_type['firefly_type']})")
        except Exception as e:
            tg_send(chat_id, f"Failed to create account: {e}")
            clear_conversation_state(telegram_user_id)
            return True
        parsed = ctx["parsed"]
        clear_conversation_state(telegram_user_id)
        attempt_post(chat_id, telegram_user_id, profile, parsed, ctx.get("original_text", ""))
        return True

    if state == "account_confirm":
        reply = text.strip().lower()
        parsed = ctx["parsed"]
        if reply in ("yes", "y", "1", "ha", "ஆம்", "हां"):
            parsed[ctx["field"]] = ctx["suggested"]
        else:
            # Treat reply as the correct account name
            parsed[ctx["field"]] = text.strip()
        clear_conversation_state(telegram_user_id)
        attempt_post(chat_id, telegram_user_id, profile, parsed, ctx.get("original_text", ""))
        return True

    if state == "newaccount_clarify":
        combined = f"{ctx.get('args','')} {text}"
        try:
            parsed_type = parse_account_type(combined)
        except Exception:
            tg_send(chat_id, "Still couldn't understand. Try: SBI savings account")
            return True
        if not parsed_type.get("clear_enough"):
            tg_send(chat_id, parsed_type.get("question") or "Please describe the account type more clearly.")
            return True
        clear_conversation_state(telegram_user_id)
        _create_account_and_notify(chat_id, profile, parsed_type)
        return True

    if state == "edit_pick_field":
        choice = text.strip()
        if choice not in EDIT_FIELDS:
            tg_send(chat_id, "Please reply with a number from 1 to 7.")
            return True
        field, field_type = EDIT_FIELDS[choice]
        new_ctx = {**ctx, "field": field, "field_type": field_type}
        cur_val = ctx["transaction"].get(field, "not set")

        if field_type == "select_type":
            tg_send(chat_id,
                f"Current: {cur_val}\n\n"
                "1. withdrawal (expense)\n"
                "2. deposit (income)\n"
                "3. transfer (between your accounts)"
            )
        elif field_type in ("account_asset", "account_all"):
            accs = get_account_names(profile, "asset" if field_type == "account_asset" else None)
            lines = [f"{i+1}. {n}" for i, n in enumerate(accs[:20])]
            tg_send(chat_id, f"Current: {cur_val}\n\n" + "\n".join(lines) + "\n\nReply with a number or type a name.")
            new_ctx["account_list"] = accs[:20]
        elif field_type == "category":
            cats = [c["attributes"]["name"] for c in ff_get_categories(profile)]
            lines = [f"{i+1}. {n}" for i, n in enumerate(cats[:20])]
            tg_send(chat_id, f"Current: {cur_val}\n\n" + "\n".join(lines) + "\n\nReply with a number or type a name.")
            new_ctx["category_list"] = cats[:20]
        else:
            tg_send(chat_id, f"Current: {cur_val}\n\nEnter new value:")

        set_conversation_state(telegram_user_id, "edit_awaiting_value", new_ctx)
        return True

    if state == "edit_awaiting_value":
        field      = ctx["field"]
        field_type = ctx["field_type"]
        new_value  = text.strip()

        if field_type == "select_type":
            type_map = {"1": "withdrawal", "2": "deposit", "3": "transfer"}
            if new_value in type_map:
                new_value = type_map[new_value]
            elif new_value not in ("withdrawal", "deposit", "transfer"):
                tg_send(chat_id, "Please reply 1, 2, or 3.")
                return True
        elif field_type in ("account_asset", "account_all"):
            acc_list = ctx.get("account_list", [])
            if new_value.isdigit() and 1 <= int(new_value) <= len(acc_list):
                new_value = acc_list[int(new_value) - 1]
        elif field_type == "category":
            cat_list = ctx.get("category_list", [])
            if new_value.isdigit() and 1 <= int(new_value) <= len(cat_list):
                new_value = cat_list[int(new_value) - 1]

        try:
            updated = dict(ctx["transaction"])
            updated[field] = new_value
            ff_update_transaction(profile, ctx["transaction_id"], {"transactions": [updated]})
            tg_send(chat_id, f"✅ Updated {field.replace('_', ' ')} to '{new_value}'.")
        except Exception as e:
            tg_send(chat_id, f"Failed to update: {e}")
        clear_conversation_state(telegram_user_id)
        return True

    return False

# ── Transaction posting with account resolution ───────────────────────────────
def attempt_post(chat_id, telegram_user_id, profile, parsed, original_text):
    txn_type = parsed.get("type", "withdrawal")

    # Which fields need account resolution and what types to search
    if txn_type == "withdrawal":
        to_check = [("source", ["asset"])]
    elif txn_type == "deposit":
        to_check = [("destination", ["asset"])]
    elif txn_type == "transfer":
        to_check = [("source", ["asset"]), ("destination", ["asset"])]
    else:
        to_check = []

    for field, types in to_check:
        name = parsed.get(field)
        if not name:
            continue
        resolved, action = resolve_account(profile, name, types)
        if action == "ambiguous":
            tg_send(chat_id, f"Did you mean '{resolved}' for {field}? (yes / or type the correct name)")
            set_conversation_state(telegram_user_id, "account_confirm", {
                "parsed": parsed, "field": field, "suggested": resolved,
                "original_text": original_text,
            })
            return
        if action == "not_found":
            tg_send(chat_id,
                f"Account '{name}' not found. What type is it?\n"
                "Examples: savings account, credit card, wallet"
            )
            set_conversation_state(telegram_user_id, "account_type_needed", {
                "parsed": parsed, "account_name": name, "field": field,
                "original_text": original_text,
            })
            return
        parsed[field] = resolved

    txn = {
        "type":        txn_type,
        "date":        parsed.get("date") or datetime.date.today().isoformat(),
        "amount":      str(parsed["amount"]),
        "description": parsed.get("description") or parsed.get("category") or "Expense",
    }
    if parsed.get("source"):
        txn["source_name"] = parsed["source"]
    if parsed.get("destination"):
        txn["destination_name"] = parsed["destination"]
    if parsed.get("category"):
        txn["category_name"] = parsed["category"]
    payload = {"transactions": [txn]}

    try:
        ff_post_transaction(profile, payload)
        tg_send(chat_id, receipt(parsed, profile["profile_name"]))
    except Exception as e:
        tg_send(chat_id, f"Failed to post to Firefly III: {e}")
        log.error("ff_post_transaction failed: %s", e)

# ── Message router ────────────────────────────────────────────────────────────
def handle_message(update):
    msg = update.get("message") or update.get("edited_message")
    if not msg or "text" not in msg:
        return

    chat_id          = msg["chat"]["id"]
    telegram_user_id = msg["from"]["id"]
    text             = msg["text"].strip()
    update_id        = update["update_id"]

    profile = get_active_profile(telegram_user_id)
    if not profile:
        tg_send(chat_id,
            "You're not registered yet.\n"
            f"Your Telegram ID: {telegram_user_id}\n"
            "Ask the admin to add your profile."
        )
        return

    # Mid-conversation reply takes priority over new commands
    state, _ = get_conversation_state(telegram_user_id)
    if state != "idle":
        if handle_conversation_reply(chat_id, telegram_user_id, profile, text):
            return

    # Command routing
    if text.startswith("/"):
        parts = text.split(None, 1)
        cmd  = parts[0].lower().split("@")[0]
        args = parts[1].strip() if len(parts) > 1 else ""

        dispatch = {
            "/start":       lambda: cmd_start(chat_id),
            "/help":        lambda: cmd_help(chat_id),
            "/balance":     lambda: cmd_balance(chat_id, profile),
            "/accounts":    lambda: cmd_accounts(chat_id, profile),
            "/categories":  lambda: cmd_categories(chat_id, profile),
            "/newaccount":  lambda: cmd_newaccount(chat_id, profile, args, telegram_user_id),
            "/recent":      lambda: cmd_recent(chat_id, profile),
            "/undo":        lambda: cmd_undo(chat_id, profile, telegram_user_id),
            "/summary":     lambda: cmd_summary(chat_id, profile),
            "/budget":      lambda: cmd_budget(chat_id, profile),
            "/find":        lambda: cmd_find(chat_id, profile, args),
            "/switch":      lambda: cmd_switch(chat_id, telegram_user_id, args),
            "/edit":        lambda: cmd_edit(chat_id, profile, telegram_user_id),
        }
        handler = dispatch.get(cmd)
        if handler:
            try:
                handler()
            except Exception as e:
                log.error("Command %s failed: %s", cmd, e)
                tg_send(chat_id, f"Something went wrong: {e}")
        else:
            tg_send(chat_id, "Unknown command. Try /help")
        return

    # Natural language → parse as transaction
    if not groq_healthy():
        queue_message(telegram_user_id, profile["id"], update_id, text)
        tg_send(chat_id, "⏳ A bit slow right now — your message is saved and will be processed shortly.")
        return

    if not firefly_healthy(profile["firefly_base_url"]):
        queue_message(telegram_user_id, profile["id"], update_id, text)
        tg_send(chat_id, "⏳ Ledger is temporarily unreachable — your message is saved and will be processed shortly.")
        return

    try:
        parsed = parse_transaction(text)
    except Exception as e:
        log.error("LLM parse failed: %s", e)
        tg_send(chat_id, "Sorry, I couldn't understand that. Please try again.")
        return

    if parsed.get("needs_clarification"):
        tg_send(chat_id, parsed["question"])
        set_conversation_state(telegram_user_id, "clarify_transaction", {
            "parsed": parsed,
            "missing_fields": parsed.get("missing_fields", []),
            "original_text": text,
        })
        return

    attempt_post(chat_id, telegram_user_id, profile, parsed, text)

# ── Retry queue ───────────────────────────────────────────────────────────────
def process_queued_messages():
    if not groq_healthy() or not firefly_healthy(FIREFLY_URL):
        return
    conn = db_conn()
    conn.autocommit = False
    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT mq.*, up.firefly_pat, up.firefly_base_url, up.profile_name
                FROM messages_queue mq
                JOIN user_profiles up ON up.id = mq.profile_id
                WHERE mq.status IN ('pending','parse_failed') AND mq.retry_count < %s
                ORDER BY mq.created_at LIMIT 20 FOR UPDATE SKIP LOCKED
            """, (MAX_RETRIES,))
            rows = cur.fetchall()
            for row in rows:
                mid = row["id"]
                cur.execute("UPDATE messages_queue SET status='processing' WHERE id=%s", (mid,))
                try:
                    parsed = parse_transaction(row["raw_text"])
                    if not parsed.get("needs_clarification") and parsed.get("amount"):
                        profile = {
                            "firefly_pat":      row["firefly_pat"],
                            "firefly_base_url": row["firefly_base_url"],
                            "profile_name":     row["profile_name"],
                        }
                        txn = {
                            "type":        parsed.get("type", "withdrawal"),
                            "date":        parsed.get("date") or datetime.date.today().isoformat(),
                            "amount":      str(parsed["amount"]),
                            "description": parsed.get("description") or parsed.get("category") or "Expense",
                        }
                        if parsed.get("source"):
                            txn["source_name"] = parsed["source"]
                        if parsed.get("destination"):
                            txn["destination_name"] = parsed["destination"]
                        if parsed.get("category"):
                            txn["category_name"] = parsed["category"]
                        payload = {"transactions": [txn]}
                        ff_post_transaction(profile, payload)
                        cur.execute(
                            "UPDATE messages_queue SET status='done', parsed_json=%s, updated_at=now() WHERE id=%s",
                            (json.dumps(parsed), mid)
                        )
                        conn.commit()
                        tg_send(row["telegram_user_id"], receipt(parsed, row["profile_name"]))
                    else:
                        # Needs clarification — cannot resolve in retry queue
                        cur.execute("UPDATE messages_queue SET status='done', updated_at=now() WHERE id=%s", (mid,))
                        conn.commit()
                except json.JSONDecodeError as e:
                    conn.rollback()
                    bump_retry(mid, "parse_failed", str(e))
                except Exception as e:
                    conn.rollback()
                    bump_retry(mid, "pending", str(e))
                    log.warning("Queue retry failed for %s: %s", mid, e)
    finally:
        conn.close()

# ── Main loop ─────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    log.info("Kanakku gateway started. Polling Telegram directly.")
    offset = get_tg_offset()
    last_queue_check = 0.0

    while True:
        try:
            updates = tg_get_updates(offset)
            for update in updates:
                try:
                    handle_message(update)
                except Exception as e:
                    log.error("handle_message crashed: %s", e)
                offset = update["update_id"] + 1
                set_tg_offset(offset)

            now = time.time()
            if now - last_queue_check >= POLL_INTERVAL:
                try:
                    process_queued_messages()
                except Exception as e:
                    log.error("process_queued_messages crashed: %s", e)
                last_queue_check = now

        except Exception as e:
            log.error("Main loop error: %s", e)
            time.sleep(5)
