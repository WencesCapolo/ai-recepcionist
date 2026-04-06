"""
app/agent/graph.py

Defines and runs the LangGraph agent loop.
This is the ONLY file in the project that imports from langgraph.

Exported interface:
    run_agent(config, history, user_message, sheets) -> tuple[str, Optional[str]]
"""

import json
import logging
import logfire
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import TypedDict, Optional, Any

from langgraph.graph import StateGraph, END
from openai import AsyncOpenAI

from app.agent.prompts import build_system_prompt
from app.agent.tools import build_tools
from app.clients.models import ClientConfig
from app.config import settings
from app.conversations.models import ConversationHistory
from app.integrations.sheets import SheetsClient

logger = logging.getLogger(__name__)

MAX_ITERATIONS = 5

# ---------------------------------------------------------------------------
# Internal state schema for the LangGraph graph
# ---------------------------------------------------------------------------

class _AgentState(TypedDict):
    messages: list[dict]          # OpenAI message dicts (mutated in-place across nodes)
    tool_defs: list[dict]         # Tool definitions in OpenAI format
    handler_map: dict             # name -> callable
    iterations: int               # safety counter
    final_reply: str              # set by agent node on text response
    _pending_tool_calls: list     # transient — tool call objects from the LLM response
    used_tools: list[str]         # keeps track of tools called


# ---------------------------------------------------------------------------
# Tool schema conversion: Anthropic → OpenAI
# Anthropic uses `input_schema`; OpenAI expects `parameters`.
# Both are plain JSON Schema objects — the content is identical.
# ---------------------------------------------------------------------------

def _to_openai_tool(anthropic_def: dict) -> dict:
    return {
        "type": "function",
        "function": {
            "name": anthropic_def["name"],
            "description": anthropic_def.get("description", ""),
            "parameters": anthropic_def["input_schema"],
        },
    }


# ---------------------------------------------------------------------------
# Graph nodes
# ---------------------------------------------------------------------------

def _make_agent_node(client: AsyncOpenAI):
    """Returns an async node function that calls the LLM."""

    async def agent_node(state: _AgentState) -> dict:
        iteration = state["iterations"]
        with logfire.span("agent_iteration", iteration=iteration):
            kwargs: dict = {
                "model": "gpt-4o-mini",
                "messages": state["messages"],
            }
            if state["tool_defs"]:
                kwargs["tools"] = state["tool_defs"]
                kwargs["tool_choice"] = "auto"

            response = await client.chat.completions.create(**kwargs)
            message = response.choices[0].message

            if message.tool_calls:
                tool_names = [tc.function.name for tc in message.tool_calls]
                logfire.info("agent_tool_calls", tools=tool_names, iteration=iteration)
                # Append the assistant message (with tool_calls) to the history.
                # OpenAI requires this before the tool result messages.
                state["messages"].append({
                    "role": "assistant",
                    "content": message.content,  # may be None
                    "tool_calls": [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {
                                "name": tc.function.name,
                                "arguments": tc.function.arguments,
                            },
                        }
                        for tc in message.tool_calls
                    ],
                })
                return {
                    "messages": state["messages"],
                    "iterations": state["iterations"],
                    "final_reply": "",
                    "_pending_tool_calls": message.tool_calls,
                    "used_tools": state.get("used_tools", []) + tool_names,
                }

            # Text response — agent is done.
            reply = (message.content or "").strip()

            stop_reason = response.choices[0].finish_reason
            logger.info(f"Agent loop iteration {iteration}, stop_reason={stop_reason}, content_types={['text' if message.content else 'None']}")

            return {
                "messages": state["messages"],
                "iterations": state["iterations"],
                "final_reply": reply,
                "_pending_tool_calls": [],
                "used_tools": state.get("used_tools", []),
            }

    return agent_node


def _make_tools_node():
    """Returns an async node function that executes all pending tool calls."""

    async def tools_node(state: _AgentState) -> dict:
        pending = state.get("_pending_tool_calls", [])

        for tc in pending:
            name = tc.function.name
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}

            with logfire.span("tool_call", tool=name, iteration=state["iterations"]):
                handler = state["handler_map"].get(name)
                if handler is None:
                    result = f"Error: herramienta '{name}' no encontrada."
                    logger.warning("Tool '%s' not found in handler_map", name)
                else:
                    try:
                        result = await handler(**args)
                    except Exception as exc:
                        result = f"Error al ejecutar la herramienta: {exc}"
                        logger.exception("Tool '%s' raised an exception", name)

            state["messages"].append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": str(result),
            })

        return {
            "messages": state["messages"],
            "iterations": state["iterations"] + 1,
            "final_reply": "",
            "_pending_tool_calls": [],
            "used_tools": state.get("used_tools", []),
        }

    return tools_node


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------

def _route_after_agent(state: _AgentState) -> str:
    """Route to tools node if the LLM made tool calls and we haven't hit the limit."""
    if state.get("_pending_tool_calls") and state["iterations"] < MAX_ITERATIONS:
        return "tools"
    return END


# ---------------------------------------------------------------------------
# Graph builder (internal)
# ---------------------------------------------------------------------------

def _build_graph(client: AsyncOpenAI):
    """Builds and compiles a minimal LangGraph StateGraph.

    Structure:
        [START] → agent → (conditional) → tools → agent
                                        ↘ END
    """
    graph = StateGraph(_AgentState)

    graph.add_node("agent", _make_agent_node(client))
    graph.add_node("tools", _make_tools_node())

    graph.set_entry_point("agent")
    graph.add_conditional_edges(
        "agent",
        _route_after_agent,
        {"tools": "tools", END: END},
    )
    graph.add_edge("tools", "agent")

    return graph.compile()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

async def run_agent(
    config: ClientConfig,
    history: ConversationHistory,
    user_message: str,
    sheets: SheetsClient,
    redis: Optional[Any] = None,
    user_phone: str = "",
) -> tuple[str, Optional[str]]:
    """Run the agent loop for a single user turn.

    Args:
        config:       Client configuration (system prompt, enabled tools, …).
        history:      Conversation history up to (but not including) user_message.
                      The caller owns persistence — this is read-only here.
        user_message: The user's new message.
        sheets:       Authenticated Google Sheets client.

    Returns:
        A tuple of (assistant's final text reply, used_tool_name).
        If multiple tools were used, they are joined by commas.

    Raises:
        Any unhandled exception from the LLM or tool layer — the caller
        (handler.py) is responsible for user-facing error messages.
    """
    # Build tool definitions and handler map for this client.
    raw_tools = build_tools(config, sheets, redis, user_phone, client_id=str(config.id))
    tool_defs = [_to_openai_tool(t["definition"]) for t in raw_tools]
    handler_map: dict = {t["definition"]["name"]: t["handler"] for t in raw_tools}

    # Build the message list from the persisted history.
    # Only user/assistant messages are stored in history; tool results are
    # ephemeral (they live only within this agent run).
    system_prompt = build_system_prompt(config)
    tz = ZoneInfo("America/Argentina/Buenos_Aires")
    now = datetime.now(tz).strftime("%A %d de %B de %Y, %H:%M")
    fresh_system = f"Fecha y hora actual en Argentina: {now}.\n\n{system_prompt}"

    messages: list[dict] = [{"role": "system", "content": fresh_system}]
    for msg in history.messages:
        if msg.role in ("user", "assistant"):
            messages.append({"role": msg.role, "content": msg.content})
    messages.append({"role": "user", "content": user_message})

    client = AsyncOpenAI(api_key=settings.openai_api_key.get_secret_value())
    graph = _build_graph(client)

    initial_state: _AgentState = {
        "messages": messages,
        "tool_defs": tool_defs,
        "handler_map": handler_map,
        "iterations": 0,
        "final_reply": "",
        "_pending_tool_calls": [],
        "used_tools": [],
    }

    result = await graph.ainvoke(initial_state)

    reply: str = result.get("final_reply", "").strip()
    if not reply:
        logger.error("Agent loop ended with empty reply. Final state: %s", result)
        raise RuntimeError("El agente no produjo una respuesta.")

    used_tools: list[str] = result.get("used_tools", [])
    tool_name = ",".join(set(used_tools)) if used_tools else None

    return reply, tool_name
