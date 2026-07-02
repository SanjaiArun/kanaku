"""LangChain tool definitions for the Kanakku agent.

Every tool receives the active Firefly III profile and the caller's
Telegram user id via `RunnableConfig["configurable"]` (injected
automatically by LangChain/LangGraph — it never appears in the schema
the LLM sees). Tools in SENSITIVE_TOOLS mutate the ledger; agent.py
routes calls to them through a human-confirmation step before they run.
"""
from typing import Optional

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import tool

import db
import firefly

SENSITIVE_TOOLS = {"create_transaction", "update_transaction", "delete_transaction", "create_account"}

ASSET_TYPES = ["asset"]
ANY_ACCOUNT_TYPES = ["asset", "expense", "revenue", "liability"]


def _profile(config: RunnableConfig) -> dict:
    return config["configurable"]["profile"]


def _telegram_user_id(config: RunnableConfig) -> int:
    return config["configurable"]["telegram_user_id"]


@tool
def list_accounts(config: RunnableConfig, account_type: Optional[str] = None) -> str:
    """List the user's Firefly III accounts and their balances.
    account_type filters to one of: asset, expense, revenue, liability.
    Leave it empty to list everything. Use this to check what accounts
    already exist, to show balances, or before creating a new account."""
    profile = _profile(config)
    accounts = firefly.get_accounts(profile, account_type)
    if not accounts:
        return "No accounts found."
    lines = []
    for a in accounts:
        attrs = a["attributes"]
        bal = attrs.get("current_balance")
        bal_str = f" — ₹{bal}" if bal is not None else ""
        lines.append(f"{attrs['name']} ({attrs['type']}){bal_str}")
    return "\n".join(lines)


@tool
def list_categories(config: RunnableConfig) -> str:
    """List the categories already set up in the user's ledger."""
    profile = _profile(config)
    cats = firefly.get_categories(profile)
    if not cats:
        return "No categories yet."
    return "\n".join(c["attributes"]["name"] for c in cats)


@tool
def resolve_account(config: RunnableConfig, name: str, account_types: str = "asset") -> str:
    """Check whether an account name the user mentioned (e.g. a bank or
    payment method) already exists, before logging a transaction or
    creating a new account. account_types is a comma-separated list drawn
    from: asset, expense, revenue, liability (default "asset" — use this
    for payment methods/banks/wallets).

    Returns one of:
    - "FOUND: <name>" — an exact or confident match exists, use this name.
    - "AMBIGUOUS: <name>" — a close but not certain match; ask the user to
      confirm before using it.
    - "NOT_FOUND" — no matching account; ask the user what type it is
      (savings account, credit card, wallet/cash, expense/merchant,
      income source) and then call create_account.
    """
    profile = _profile(config)
    types = [t.strip() for t in account_types.split(",") if t.strip()]
    resolved, status = firefly.resolve_account(profile, name, types)
    if status == "found":
        return f"FOUND: {resolved}"
    if status == "ambiguous":
        return f"AMBIGUOUS: {resolved}"
    return "NOT_FOUND"


@tool
def create_account(config: RunnableConfig, name: str, firefly_type: str,
                    account_role: Optional[str] = None) -> str:
    """Create a new Firefly III account. This changes the user's ledger,
    so only call it after resolve_account returned NOT_FOUND and the user
    confirms the details.
    firefly_type: one of asset, expense, revenue, liability.
    account_role (only for type=asset): one of defaultAsset, savingAsset,
    ccAsset, cashWalletAsset."""
    profile = _profile(config)
    created = firefly.create_account(profile, name, firefly_type, account_role)
    return f"Created account '{created['attributes']['name']}' ({firefly_type})."


@tool
def create_transaction(config: RunnableConfig, type: str, amount: float,
                        source: Optional[str] = None, destination: Optional[str] = None,
                        category: Optional[str] = None, description: Optional[str] = None,
                        date: Optional[str] = None) -> str:
    """Log a transaction in the ledger. This changes the user's ledger.
    type: withdrawal (an expense), deposit (income), or transfer (between
    the user's own accounts).
    - withdrawal: source = the payment account/method used; destination =
      optional, what it was spent on.
    - deposit: destination = the account the money landed in; source =
      optional, where it came from.
    - transfer: both source and destination are required asset accounts.
    Resolve source/destination with resolve_account first so the names
    match existing accounts exactly. amount must be a positive number.
    date defaults to today if omitted (format YYYY-MM-DD)."""
    if amount is None or amount <= 0:
        return f"Rejected: amount must be a positive number greater than zero (got {amount!r})."
    profile = _profile(config)
    import datetime as _dt
    txn = {
        "type": type,
        "date": date or _dt.date.today().isoformat(),
        "amount": str(amount),
        "description": description or category or type.capitalize(),
    }
    if source:
        txn["source_name"] = source
    if destination:
        txn["destination_name"] = destination
    if category:
        txn["category_name"] = category
    created = firefly.post_transaction(profile, {"transactions": [txn]})
    return f"Posted: {firefly.format_transaction(created)}"


@tool
def list_recent_transactions(config: RunnableConfig, limit: int = 5) -> str:
    """List the most recent transactions in the current ledger."""
    profile = _profile(config)
    txns = firefly.get_recent_transactions(profile, limit)
    if not txns:
        return "No recent transactions."
    return "\n".join(firefly.format_transaction(t) for t in txns)


@tool
def search_transactions(config: RunnableConfig, query: str) -> str:
    """Search past transactions by keyword (merchant, description, category)."""
    profile = _profile(config)
    txns = firefly.search_transactions(profile, query)
    if not txns:
        return f"No transactions found for '{query}'."
    return "\n".join(firefly.format_transaction(t) for t in txns)


@tool
def get_monthly_summary(config: RunnableConfig) -> str:
    """Get this month's spending grouped by category."""
    profile = _profile(config)
    data = firefly.get_monthly_summary(profile)
    if not data:
        return "No spending data this month."
    total = 0.0
    lines = []
    for item in data:
        amount = abs(float(item.get("difference_float", 0)))
        total += amount
        lines.append(f"{item.get('name','?')}: ₹{amount:.0f}")
    lines.append(f"Total: ₹{total:.0f}")
    return "\n".join(lines)


@tool
def get_budgets(config: RunnableConfig) -> str:
    """Get budget limits and how much of each has been spent this period."""
    profile = _profile(config)
    budgets = firefly.get_budgets(profile)
    if not budgets:
        return "No budgets set up."
    lines = []
    for b in budgets:
        attrs = b["attributes"]
        spent = abs(float((attrs.get("spent") or [{}])[0].get("sum", 0)))
        limit_val = float(attrs["auto_budget_amount"]) if attrs.get("auto_budget_amount") else None
        if limit_val:
            lines.append(f"{attrs['name']}: ₹{spent:.0f} / ₹{limit_val:.0f} (₹{limit_val - spent:.0f} left)")
        else:
            lines.append(f"{attrs['name']}: ₹{spent:.0f} spent (no limit set)")
    return "\n".join(lines)


@tool
def update_transaction(config: RunnableConfig, transaction_id: str, field: str, value: str) -> str:
    """Edit a field on an existing transaction. This changes the user's
    ledger. field: one of amount, source_name, destination_name,
    category_name, description, date, type. Use list_recent_transactions
    or search_transactions first to find the transaction_id."""
    profile = _profile(config)
    txns = firefly.get_recent_transactions(profile, 50)
    match = next((t for t in txns if str(t["id"]) == str(transaction_id)), None)
    if not match:
        return f"Transaction {transaction_id} not found in recent history."
    attrs = dict(firefly.txn_attrs(match))
    attrs[field] = value
    updated = firefly.update_transaction(profile, transaction_id, {"transactions": [attrs]})
    return f"Updated: {firefly.format_transaction(updated)}"


@tool
def delete_transaction(config: RunnableConfig, transaction_id: str) -> str:
    """Delete a transaction from the ledger. This cannot be undone. Use
    list_recent_transactions or search_transactions first to find the
    transaction_id."""
    profile = _profile(config)
    firefly.delete_transaction(profile, transaction_id)
    return f"Deleted transaction {transaction_id}."


@tool
def list_profiles(config: RunnableConfig) -> str:
    """List the user's Firefly profiles (e.g. personal, business) and
    which one is currently active."""
    telegram_user_id = _telegram_user_id(config)
    profiles = db.get_all_profiles(telegram_user_id)
    if not profiles:
        return "No profiles found."
    return "\n".join(
        f"{'* ' if p['is_active'] else '  '}{p['profile_name']}" for p in profiles
    )


@tool
def switch_profile(config: RunnableConfig, profile_name: str) -> str:
    """Switch the active profile — all future transactions post to this
    profile's ledger until switched again."""
    telegram_user_id = _telegram_user_id(config)
    if db.switch_profile(telegram_user_id, profile_name.strip()):
        return f"Switched to profile '{profile_name.strip()}'."
    return f"Profile '{profile_name.strip()}' not found. Use list_profiles to see available profiles."


ALL_TOOLS = [
    list_accounts,
    list_categories,
    resolve_account,
    create_account,
    create_transaction,
    list_recent_transactions,
    search_transactions,
    get_monthly_summary,
    get_budgets,
    update_transaction,
    delete_transaction,
    list_profiles,
    switch_profile,
]
