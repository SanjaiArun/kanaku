"""Firefly III REST API client used by the agent's tools."""
import datetime
import difflib

import requests


def _headers(profile):
    return {
        "Authorization": f"Bearer {profile['firefly_pat']}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }


def get_accounts(profile, account_type=None):
    params = {"limit": 200}
    if account_type:
        params["type"] = account_type
    r = requests.get(f"{profile['firefly_base_url']}/api/v1/accounts",
                      headers=_headers(profile), params=params, timeout=10)
    r.raise_for_status()
    return r.json().get("data", [])


def get_account_names(profile, account_type=None):
    return [a["attributes"]["name"] for a in get_accounts(profile, account_type)]


def create_account(profile, name, account_type, account_role=None):
    payload = {
        "name": name,
        "type": account_type,
        "opening_balance": "0",
        "opening_balance_date": datetime.date.today().isoformat(),
    }
    if account_role:
        payload["account_role"] = account_role
    r = requests.post(f"{profile['firefly_base_url']}/api/v1/accounts",
                       headers=_headers(profile), json=payload, timeout=10)
    r.raise_for_status()
    return r.json()["data"]


def get_categories(profile):
    r = requests.get(f"{profile['firefly_base_url']}/api/v1/categories",
                      headers=_headers(profile), params={"limit": 200}, timeout=10)
    r.raise_for_status()
    return r.json().get("data", [])


def post_transaction(profile, payload):
    r = requests.post(f"{profile['firefly_base_url']}/api/v1/transactions",
                       headers=_headers(profile), json=payload, timeout=15)
    r.raise_for_status()
    return r.json()["data"]


def get_recent_transactions(profile, limit=5):
    r = requests.get(f"{profile['firefly_base_url']}/api/v1/transactions",
                      headers=_headers(profile), params={"limit": limit, "type": "default"}, timeout=10)
    r.raise_for_status()
    return r.json().get("data", [])


def delete_transaction(profile, transaction_id):
    r = requests.delete(f"{profile['firefly_base_url']}/api/v1/transactions/{transaction_id}",
                         headers=_headers(profile), timeout=10)
    r.raise_for_status()


def update_transaction(profile, transaction_id, payload):
    r = requests.put(f"{profile['firefly_base_url']}/api/v1/transactions/{transaction_id}",
                      headers=_headers(profile), json=payload, timeout=15)
    r.raise_for_status()
    return r.json()["data"]


def get_monthly_summary(profile):
    today = datetime.date.today()
    start = today.replace(day=1).isoformat()
    r = requests.get(f"{profile['firefly_base_url']}/api/v1/insight/expense/category",
                      headers=_headers(profile),
                      params={"start": start, "end": today.isoformat()}, timeout=10)
    r.raise_for_status()
    return r.json()


def get_budgets(profile):
    r = requests.get(f"{profile['firefly_base_url']}/api/v1/budgets",
                      headers=_headers(profile), params={"limit": 50}, timeout=10)
    r.raise_for_status()
    return r.json().get("data", [])


def search_transactions(profile, query, limit=5):
    r = requests.get(f"{profile['firefly_base_url']}/api/v1/search/transactions",
                      headers=_headers(profile), params={"query": query, "limit": limit}, timeout=10)
    r.raise_for_status()
    return r.json().get("data", [])


def service_up(url):
    try:
        return requests.get(url, timeout=3).status_code < 500
    except requests.RequestException:
        return False


def healthy(base_url):
    return service_up(f"{base_url}/health")


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
    """Returns (resolved_name, status): status is 'found' | 'ambiguous' | 'not_found'."""
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


def txn_attrs(t):
    a = t.get("attributes", {})
    splits = a.get("transactions")
    return splits[0] if splits else a


def format_transaction(t):
    a = txn_attrs(t)
    return (f"#{t.get('id','?')}: ₹{a.get('amount','?')} {a.get('type','?')} — "
            f"{a.get('description') or a.get('category_name') or '?'} "
            f"({a.get('source_name','?')} → {a.get('destination_name','?')}) "
            f"on {str(a.get('date','?'))[:10]}")
