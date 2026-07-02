# Kanakku

*"Kanakku" (கணக்கு) — Tamil for "account" / "the calculation."*

A multi-user personal finance assistant. Send an expense, income, or transfer
as a Telegram message in English, Tamil, or Hindi; Groq parses it; it lands
in your own self-hosted Firefly III ledger.

## Architecture

```
   Telegram message ───▶ kanakku_gateway (polls getUpdates every 30s)
                                │
                                ├──▶ Groq API (parse natural language → JSON)
                                │
                                ├──▶ Firefly III (post transaction)
                                │
                                └──▶ Telegram (send receipt or ask clarification)
```

Services (all in `docker-compose.yml`):

| Service | Role |
|---|---|
| `postgres` | Two logical DBs: `firefly` (ledger) and `kanakku` (queue, profiles, conversation state) |
| `firefly_iii` + `firefly_cron` | Self-hosted finance ledger, multi-user |
| `kanakku_gateway` | Polls Telegram, parses with Groq, posts to Firefly, handles conversations |

## What it understands

Natural language in English, Tamil, or Hindi:

```
spent 500 on food using SBI
got salary 50000 in HDFC
transferred 1000 from SBI to HDFC
```

If something is missing or unclear, the bot asks — and never acts on a guess.

## Commands

| Command | What it does |
|---|---|
| `/accounts` | List all your Firefly accounts with balances |
| `/categories` | List all categories |
| `/newaccount SBI savings account` | Create an account (detects type from description) |
| `/switch business` | Switch active profile |
| `/help` | Show all commands |

## Multi-user design

Each Telegram user maps to one or more **profiles**, each with its own
Firefly III Personal Access Token (PAT). Exactly one profile is active at a
time — all messages post to the active profile.

**Registering a profile:**
```bash
docker compose exec postgres psql -U kanakku_admin -d kanakku -c \
  "INSERT INTO user_profiles (telegram_user_id, profile_name, firefly_pat, firefly_base_url, is_active)
   VALUES (<telegram_id>, 'personal', '<pat>', 'http://firefly_iii:8080', TRUE);"
```

**Switching profiles:** use `/switch <profile_name>` in Telegram.

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

### Step 4 — Register your Telegram profile
```bash
docker compose exec postgres psql -U kanakku_admin -d kanakku -c \
  "INSERT INTO user_profiles (telegram_user_id, profile_name, firefly_pat, firefly_base_url, is_active)
   VALUES (<your_telegram_id>, 'personal', '<your_pat>', 'http://firefly_iii:8080', TRUE);"
```

Get your Telegram user ID by messaging [@userinfobot](https://t.me/userinfobot).

### Step 5 — Start the gateway
```bash
docker compose up -d --build kanakku_gateway
docker compose logs -f kanakku_gateway
```

Send a message to your bot and watch it work.

## Fallback handling

| Failure | What Kanakku does |
|---|---|
| **Groq unreachable** | Bot replies asking to try again; message not lost |
| **Firefly III unreachable** | Bot replies asking to try again |
| **LLM returns ambiguous result** | Bot asks clarifying question, waits for reply |
| **Account not found** | Bot asks for account type, creates it, retries |
| **Duplicate Telegram update** | `telegram_update_id` is `UNIQUE` — duplicate ignored |
| **DB restarting** | Healthchecks ensure gateway waits for Postgres before starting |
