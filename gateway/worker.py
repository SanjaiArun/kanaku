"""
Kanakku Gateway
===============
Polls Telegram directly (no n8n required) and routes every message through
a LangGraph agent (see agent.py) that decides for itself whether to ask a
clarifying question, ask for confirmation before changing the ledger, or
just answer. Per-user conversation state (including paused
confirmations) is persisted in Postgres via LangGraph's checkpointer, so
it survives gateway restarts.
"""
import logging
import os
import time

import requests
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.types import Command

import db
from agent import build_graph

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("kanakku")

TG_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TG_API = f"https://api.telegram.org/bot{TG_TOKEN}"
POLL_TIMEOUT_SECONDS = 20

START_TEXT = (
    "Welcome to Kanakku!\n\n"
    "Kanakku (கணக்கு) is your personal finance assistant. Just tell me "
    "what happened, in English, Tamil, or Hindi, and I'll log it, ask if "
    "I'm missing something, and confirm before I change anything.\n\n"
    "Examples:\n"
    "  spent 500 on food using SBI\n"
    "  got salary 50000 in HDFC\n"
    "  transferred 2000 from SBI to HDFC\n\n"
    "You can also ask things like:\n"
    "  what's my balance?\n"
    "  show my recent transactions\n"
    "  switch to my business profile\n\n"
    "/help — show this message again"
)


def tg_get_updates(offset):
    try:
        r = requests.get(f"{TG_API}/getUpdates",
                          params={"offset": offset, "timeout": POLL_TIMEOUT_SECONDS},
                          timeout=POLL_TIMEOUT_SECONDS + 5)
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


def _last_reply(result: dict) -> str | None:
    interrupts = result.get("__interrupt__")
    if interrupts:
        return interrupts[0].value["question"]
    for message in reversed(result.get("messages", [])):
        if isinstance(message, AIMessage) and message.content:
            return message.content
    return None


def handle_message(update, graph):
    msg = update.get("message") or update.get("edited_message")
    if not msg or "text" not in msg:
        return

    chat_id = msg["chat"]["id"]
    telegram_user_id = msg["from"]["id"]
    text = msg["text"].strip()

    if text in ("/start", "/help"):
        tg_send(chat_id, START_TEXT)
        return

    profile = db.get_active_profile(telegram_user_id)
    if not profile:
        tg_send(chat_id,
                "You're not registered yet.\n"
                f"Your Telegram ID: {telegram_user_id}\n"
                "Ask the admin to add your profile.")
        return

    config = {
        "configurable": {
            "thread_id": str(telegram_user_id),
            "profile": profile,
            "telegram_user_id": telegram_user_id,
        }
    }

    try:
        snapshot = graph.get_state(config)
        if snapshot.next:
            result = graph.invoke(Command(resume=text), config=config)
        else:
            result = graph.invoke({"messages": [HumanMessage(text)]}, config=config)
    except Exception as e:
        log.error("Agent invocation failed for user %s: %s", telegram_user_id, e)
        tg_send(chat_id, "Something went wrong on my end — please try again in a moment.")
        return

    reply = _last_reply(result)
    if reply:
        tg_send(chat_id, reply)


def main():
    log.info("Kanakku gateway started. Polling Telegram directly.")
    with PostgresSaver.from_conn_string(db.conn_string()) as checkpointer:
        checkpointer.setup()
        graph = build_graph(checkpointer)

        offset = db.get_tg_offset()
        while True:
            try:
                updates = tg_get_updates(offset)
                for update in updates:
                    try:
                        handle_message(update, graph)
                    except Exception as e:
                        log.error("handle_message crashed: %s", e)
                    offset = update["update_id"] + 1
                    db.set_tg_offset(offset)
            except Exception as e:
                log.error("Main loop error: %s", e)
                time.sleep(5)


if __name__ == "__main__":
    main()
