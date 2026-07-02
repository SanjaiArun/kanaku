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
- Call at most one tool per reply — never batch multiple tool calls.
- Every tool that changes the ledger (create_transaction, create_account,
  update_transaction, delete_transaction) already pauses for the user's
  explicit confirmation before it runs, so just call it once you have
  everything you need — you don't need to ask "shall I proceed?" yourself
  first, the tool will.
- If the user declines or asks to change something, adjust and try again;
  never insist.
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


def build_graph(checkpointer):
    model = ChatGroq(model=GROQ_MODEL, temperature=0)
    model_with_tools = model.bind_tools(ALL_TOOLS)

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
        if any(tc["name"] in SENSITIVE_TOOLS for tc in tool_calls):
            return "human_review"
        return "tools"

    def human_review_node(state: AgentState) -> Command[Literal["tools", "agent"]]:
        last: AIMessage = state["messages"][-1]
        tool_call = last.tool_calls[0]
        reply = interrupt({"question": _describe_action(tool_call)})
        if _is_confirmation(reply):
            return Command(goto="tools")
        tool_message = ToolMessage(
            content=(
                f"The user did not confirm this action. They said: '{reply}'. "
                "Do not perform the action; work out what they want instead."
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
