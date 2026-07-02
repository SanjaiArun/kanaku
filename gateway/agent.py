"""The Kanakku LangGraph agent.

Replaces the old hand-rolled conversation_state FSM. The agent reads the
full conversation, decides for itself whether it has enough information
to act or needs to ask the user a question, and — before any tool in
tools.SENSITIVE_TOOLS actually runs — pauses the graph and asks the user
to confirm a plain-language summary of the action. The pause/resume is
implemented with LangGraph's `interrupt`/`Command(resume=...)`, persisted
per Telegram user (thread_id) by a Postgres checkpointer, so it survives
gateway restarts.
"""
import os
from typing import Annotated, Literal

from typing_extensions import TypedDict
from langchain_core.messages import AIMessage, SystemMessage, ToolMessage
from langchain_groq import ChatGroq
from langgraph.graph import StateGraph, START, END
from langgraph.graph.message import add_messages
from langgraph.prebuilt import ToolNode
from langgraph.types import Command, interrupt

from tools import ALL_TOOLS, SENSITIVE_TOOLS

GROQ_MODEL = os.environ.get("GROQ_MODEL", "llama-3.1-8b-instant")

# Each Groq model has its own separate daily token quota. When the
# primary model's quota (or per-minute rate limit) is exhausted, Groq
# returns a 429 — fall through to the next model in this list rather
# than failing the turn. Order matters: put stronger tool-callers first
# so quality only degrades once quota actually forces a fallback.
GROQ_MODEL_FALLBACKS = [
    m.strip() for m in os.environ.get(
        "GROQ_MODEL_FALLBACKS",
        "llama-3.3-70b-versatile,openai/gpt-oss-20b,meta-llama/llama-4-scout-17b-16e-instruct",
    ).split(",") if m.strip()
]

# Cap how many past messages are replayed to the model each turn, so a
# long-running conversation doesn't grow the prompt (and the bill)
# without bound.
MAX_HISTORY_MESSAGES = 40

SYSTEM_PROMPT = """You are Kanakku (கணக்கு), a multi-user personal finance
assistant reachable over Telegram. Users write in English, Tamil, or
Hindi — always reply in the same language they used.

You have tools to read the user's Firefly III ledger (accounts,
categories, transactions, budgets, summaries) and to change it (log a
transaction, create an account, edit or delete a transaction, switch the
active profile).

How to behave:
- Have a real conversation. If a message is missing something you need
  (e.g. the amount, or which account/payment method was used), ask a
  single, specific question instead of guessing.
- Before logging a transaction, use resolve_account to check whether the
  account name the user gave you already exists. If it's NOT_FOUND, ask
  what type of account it is (savings account, credit card, wallet/cash,
  a shop/merchant, or an income source) and create it with create_account
  before logging the transaction. If it's AMBIGUOUS, confirm the closest
  match with the user before proceeding.
- Call at most one tool per reply — never batch multiple tool calls in the
  same turn, even if you think you know what the next one will be.
- Every tool that changes the ledger (create_transaction, create_account,
  update_transaction, delete_transaction) already pauses for the user's
  explicit confirmation before it runs, so just call it once you have
  everything you need — you don't need to ask "shall I proceed?" yourself
  first, the tool will.
- If the user declines or asks to change something, adjust and try again;
  never insist.
- Never answer a question about real data (transactions, balances,
  accounts, categories, budgets, summaries, search results) from memory
  or by guessing. Always call the matching tool first and base your reply
  only on what it returns. If a tool call fails or you're unsure, say so
  instead of making something up.
- Keep replies short and concrete. Use ₹ for amounts.
- Today's date is used automatically when a transaction's date is omitted.
"""


class AgentState(TypedDict):
    messages: Annotated[list, add_messages]


def _describe_action(tool_call: dict) -> str:
    name, args = tool_call["name"], tool_call["args"]

    if name == "create_transaction":
        parts = [f"{str(args.get('type', '?')).capitalize()} of ₹{args.get('amount', '?')}"]
        if args.get("source"):
            parts.append(f"from {args['source']}")
        if args.get("destination"):
            parts.append(f"to {args['destination']}")
        if args.get("category"):
            parts.append(f"category: {args['category']}")
        if args.get("description"):
            parts.append(f"note: {args['description']}")
        date = args.get("date") or "today"
        return (f"Log this transaction?\n{', '.join(parts)} ({date})\n\n"
                "Reply 'yes' to confirm, or tell me what to change.")

    if name == "create_account":
        role = f" ({args['account_role']})" if args.get("account_role") else ""
        return (f"Create a new account '{args.get('name')}' as {args.get('firefly_type')}{role}?\n\n"
                "Reply 'yes' to confirm, or tell me what to change.")

    if name == "update_transaction":
        return (f"Update transaction #{args.get('transaction_id')}: "
                f"set {args.get('field')} = '{args.get('value')}'?\n\n"
                "Reply 'yes' to confirm, or tell me what to change.")

    if name == "delete_transaction":
        return (f"Delete transaction #{args.get('transaction_id')}? This cannot be undone.\n\n"
                "Reply 'yes' to confirm.")

    return f"Confirm this action: {name}({args})?\n\nReply 'yes' to confirm, or tell me what to change."


def _is_confirmation(reply: str) -> bool:
    return reply.strip().lower() in (
        "yes", "y", "ok", "okay", "confirm", "confirmed", "sure", "go ahead", "do it",
        "ஆம்", "சரி", "हां", "हाँ", "ठीक है",
    )


def _validate_tool_call(tool_call: dict) -> str | None:
    """Returns an error message if a sensitive tool call's args are
    obviously bad, so it never reaches the user as a confirmation prompt
    at all. Returns None if the call looks fine."""
    if tool_call["name"] == "create_transaction":
        amount = tool_call["args"].get("amount")
        try:
            if amount is None or float(amount) <= 0:
                return (
                    "Rejected: amount must be a positive number greater than zero "
                    f"(got {amount!r}). Ask the user for the correct amount — do not "
                    "guess a sign or transaction type to make a bad amount work."
                )
        except (TypeError, ValueError):
            return f"Rejected: amount {amount!r} is not a valid number. Ask the user for the correct amount."
    return None


def _build_model_chain():
    # Dedup while preserving order: primary first, then each fallback
    # that isn't already in the list.
    model_names = [GROQ_MODEL] + [m for m in GROQ_MODEL_FALLBACKS if m != GROQ_MODEL]
    bound = [
        ChatGroq(model=name, temperature=0).bind_tools(ALL_TOOLS, parallel_tool_calls=False)
        for name in model_names
    ]
    if len(bound) == 1:
        return bound[0]
    # Best-effort: ask the model not to batch tool calls. Not every Groq
    # model honors this, which is why route_after_agent/human_review_node
    # below defend against batching regardless.
    return bound[0].with_fallbacks(bound[1:])


def build_graph(checkpointer):
    model_with_tools = _build_model_chain()

    def agent_node(state: AgentState):
        messages = state["messages"][-MAX_HISTORY_MESSAGES:]
        messages = [SystemMessage(SYSTEM_PROMPT)] + messages
        response = model_with_tools.invoke(messages)
        return {"messages": [response]}

    def route_after_agent(state: AgentState) -> Literal["human_review", "tools", "__end__"]:
        last = state["messages"][-1]
        tool_calls = getattr(last, "tool_calls", None)
        if not tool_calls:
            return END
        # Route through human_review whenever there's more than one call
        # (even if none look sensitive) or any single one is sensitive —
        # never let ToolNode execute a batch that might contain a
        # mutating call we never showed the user.
        if len(tool_calls) > 1 or any(tc["name"] in SENSITIVE_TOOLS for tc in tool_calls):
            return "human_review"
        return "tools"

    def human_review_node(state: AgentState) -> Command[Literal["tools", "agent"]]:
        last: AIMessage = state["messages"][-1]
        tool_calls = last.tool_calls

        if len(tool_calls) > 1:
            # The model batched multiple tool calls in one turn despite
            # being told not to. Refuse the whole batch — every tool_call
            # in this AIMessage needs a matching ToolMessage before the
            # next model call, or the Groq API will reject the request.
            rejections = [
                ToolMessage(
                    content=(
                        "Rejected: you must call exactly one tool per turn. "
                        "Call this one again by itself once it's actually needed."
                    ),
                    tool_call_id=tc["id"],
                )
                for tc in tool_calls
            ]
            return Command(goto="agent", update={"messages": rejections})

        tool_call = tool_calls[0]
        if tool_call["name"] not in SENSITIVE_TOOLS:
            # A lone non-sensitive call only reaches here if it happened
            # to be routed alongside a since-rejected batch; just run it.
            return Command(goto="tools")

        validation_error = _validate_tool_call(tool_call)
        if validation_error:
            # Bad args (e.g. a non-positive amount) never even reach the
            # user as a confirmation prompt — bounce straight back to the
            # agent to ask a real question instead of confirming garbage.
            return Command(goto="agent", update={"messages": [
                ToolMessage(content=validation_error, tool_call_id=tool_call["id"])
            ]})

        reply = interrupt({"question": _describe_action(tool_call)})
        if _is_confirmation(reply):
            return Command(goto="tools")
        tool_message = ToolMessage(
            content=(
                f"The user did not reply 'yes' to confirm. They said: '{reply}'. "
                "This could mean two different things — figure out which from "
                "context: (1) they want to change a detail of the pending action "
                "(amount, account, category, etc.) — adjust and re-propose it, or "
                "(2) what they said is a completely new, unrelated request that "
                "has nothing to do with the pending action — if so, drop the "
                "pending action without asking about it again and just handle "
                "the new request normally."
            ),
            tool_call_id=tool_call["id"],
        )
        return Command(goto="agent", update={"messages": [tool_message]})

    graph = StateGraph(AgentState)
    graph.add_node("agent", agent_node)
    graph.add_node("tools", ToolNode(ALL_TOOLS))
    graph.add_node("human_review", human_review_node)
    graph.add_edge(START, "agent")
    graph.add_conditional_edges(
        "agent", route_after_agent, {"human_review": "human_review", "tools": "tools", END: END}
    )
    graph.add_edge("tools", "agent")

    return graph.compile(checkpointer=checkpointer)
