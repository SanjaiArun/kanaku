# Kanakku

*"Kanakku" (கணக்கு) — Tamil for "account" / "the calculation."*

A multi-user personal finance assistant. Talk to it on Telegram in English,
Tamil, or Hindi — describe an expense, income, or transfer, ask about your
balances, or ask it to fix a past entry. A LangGraph agent decides what to
do: it asks for anything it's missing, and always shows a plain-language
summary and asks you to confirm before it changes anything in your ledger.

## Architecture

```
   Telegram message ──▶ kanakku_gateway (polls getUpdates)
                              │
                              ▼
                     LangGraph agent (Groq LLM + tools)
                              │
              ┌───────────────┼────────────────────┐
              ▼               ▼                    ▼
      read-only tools   confirm with user    Firefly III (post/edit/
   (balances, recent,   before any mutating   delete transactions,
    search, budgets)         tool runs         create accounts)
```

Services (all in `docker-compose.yml`):

| Service | Role |
|---|---|
| `postgres` | Two logical DBs: `firefly` (ledger) and `kanakku` (profiles + the agent's conversation memory) |
| `firefly_iii` + `firefly_cron` | Self-hosted finance ledger, multi-user |
| `kanakku_gateway` | Polls Telegram, runs the LangGraph agent, talks to Firefly |

The gateway has no fixed conversation script. The agent (built with
LangChain + LangGraph) reads the whole conversation, calls read-only tools
freely to look things up, and asks a clarifying question whenever it
doesn't have what it needs (e.g. an amount, or which account was used).
Before any tool that changes the ledger — logging a transaction, creating
an account, editing or deleting a transaction — actually runs, the graph
pauses (via LangGraph's `interrupt`), shows you a summary of exactly what
it's about to do, and waits for you to confirm or redirect it. This state
is checkpointed in Postgres per Telegram user, so it survives gateway
restarts.

## What it understands

Natural language in English, Tamil, or Hindi:

```
spent 500 on food using SBI
got salary 50000 in HDFC
transferred 1000 from SBI to HDFC
what's my balance?
show my recent transactions
switch to my business profile
```

If something is missing or unclear, the bot asks — and it always confirms
before posting, editing, or deleting anything.

`/start` and `/help` show a quick intro; everything else is just conversation.

## Multi-user design

Each Telegram user gets up to two **profiles** — `personal` and
`business` — each a fully separate, isolated Firefly III account with
its own Personal Access Token. Exactly one profile is active at a time;
all messages post to the active one.

**No registration step.** The first time a new Telegram user messages
the bot, their `personal` profile is created automatically — a real,
dedicated Firefly III user account is provisioned behind the scenes
(see `gateway/firefly_admin.py`). Nothing for you or them to do.

**Adding a business profile:** just tell the bot, e.g. "set up a
business profile" — also instant, no confirmation needed since it
doesn't touch money. Each user can have at most one of each.

**Switching profiles:** just tell the bot, e.g. "switch to business".

## Setup

### Step 1 — Configuration
```bash
cp .env.example .env
# Fill in: POSTGRES_ADMIN_PASSWORD, FIREFLY_APP_KEY, FIREFLY_STATIC_CRON_TOKEN,
#          TELEGRAM_BOT_TOKEN, GROQ_API_KEY
```

Get a free Groq API key at [console.groq.com](https://console.groq.com).

Generate `FIREFLY_APP_KEY` (PowerShell):
```powershell
$rng = [System.Security.Cryptography.RandomNumberGenerator]::Create()
$bytes = New-Object byte[] 32; $rng.GetBytes($bytes)
"base64:" + [Convert]::ToBase64String($bytes)
```

### Step 2 — Start infrastructure
```bash
docker compose up -d postgres firefly_iii firefly_cron
docker compose logs -f postgres   # confirm DBs created
```

### Step 3 — Firefly III first-time setup
1. Open [http://localhost:8080](http://localhost:8080) and register your first user
2. Set default currency to **INR**
3. Go to [http://localhost:8080/profile/oauth](http://localhost:8080/profile/oauth)
4. Create a **Personal Access Token** — copy it immediately

### Step 4 — One-time admin setup

The account you just registered in Step 3 is automatically the Firefly
owner/admin. Use the same PAT from Step 3 (or generate another one the
same way) and put it in `.env` as `FIREFLY_ADMIN_PAT` — the gateway
uses it to auto-provision a real, isolated Firefly account for every
new Telegram user, with no manual step per person.

### Step 5 — Start the gateway
```bash
docker compose up -d --build kanakku_gateway
docker compose logs -f kanakku_gateway
```

Message your bot — your `personal` profile is created automatically on
first contact.

## Fallback handling

| Failure | What Kanakku does |
|---|---|
| **Groq unreachable** | Bot replies asking to try again; nothing is posted |
| **Firefly III unreachable** | The failing tool call is reported back to the agent, which tells the user and lets them retry |
| **Message unclear or missing details** | Agent asks a specific clarifying question and waits for the reply |
| **Account not found** | Agent asks what type it is, creates it (with your confirmation), then continues |
| **DB restarting** | Healthchecks ensure gateway waits for Postgres before starting |

Every mutating action — logging, editing, or deleting a transaction, or
creating an account — is confirmed with the user before it happens, so a
misunderstood message never silently changes the ledger.
