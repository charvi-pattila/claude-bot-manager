"""Anthropic client, tool definitions, and the tool-use loop.

Two things here are deliberate and worth knowing before editing:

1. The loop is **multi-round**. The previous version handled exactly one round
   of tool use, so if the model wanted a second tool call after seeing the first
   result, that call was silently dropped and the user got a confusing reply.

2. ``send_email`` is **never executed here**. It parks the turn and hands the
   proposed message back to the caller for explicit confirmation. An LLM with a
   live send-mail capability and user-authored instructions is a prompt-injection
   target; a human has to look at the actual recipient and body before anything
   leaves the account.
"""

from __future__ import annotations

from typing import Any

import anthropic

import config

EMAIL_TOOLS: list[dict[str, Any]] = [
    {
        "name": "send_email",
        "description": (
            "Propose an email to send via Gmail on behalf of the user. The email is "
            "NOT sent immediately: the user is shown the recipient, subject, and body "
            "and must confirm before it is delivered."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "to": {"type": "string", "description": "Recipient email address"},
                "subject": {"type": "string", "description": "Email subject line"},
                "body": {"type": "string", "description": "Email body (plain text)"},
            },
            "required": ["to", "subject", "body"],
            "additionalProperties": False,
        },
    }
]


class MissingAPIKey(RuntimeError):
    """Raised when no ANTHROPIC_API_KEY is configured."""


def get_client() -> anthropic.Anthropic:
    if not config.ANTHROPIC_API_KEY:
        raise MissingAPIKey(
            "ANTHROPIC_API_KEY is not set. Add it to your .env or your host's "
            "environment variables."
        )
    return anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)


def extract_text(content: list[Any]) -> str:
    """Join every text block.

    The old code read ``content[0].text``, which breaks as soon as block 0 is a
    thinking or tool_use block — and on current models it often is.
    """
    parts = []
    for block in content:
        if getattr(block, "type", None) == "text":
            parts.append(block.text)
    return "\n".join(p for p in parts if p).strip()


def blocks_to_params(content: list[Any]) -> list[dict[str, Any]]:
    """Convert response blocks into JSON-serializable request params.

    Serializable matters: the conversation is persisted to the database while a
    turn waits for email confirmation, and resumed by whichever worker picks it
    up.
    """
    out: list[dict[str, Any]] = []
    for block in content:
        btype = getattr(block, "type", None)
        if btype == "text":
            out.append({"type": "text", "text": block.text})
        elif btype == "tool_use":
            out.append(
                {
                    "type": "tool_use",
                    "id": block.id,
                    "name": block.name,
                    "input": block.input,
                }
            )
        elif btype == "thinking":
            # Echoed back unchanged; the API rejects modified thinking blocks.
            out.append(
                {
                    "type": "thinking",
                    "thinking": getattr(block, "thinking", ""),
                    "signature": getattr(block, "signature", ""),
                }
            )
    return out


def tool_uses(content: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [b for b in content if b.get("type") == "tool_use"]


def _call(client: anthropic.Anthropic, system: str, conversation: list[dict[str, Any]]):
    kwargs: dict[str, Any] = {
        "model": config.CHAT_MODEL,
        "max_tokens": config.MAX_TOKENS,
        "messages": conversation,
        "tools": EMAIL_TOOLS,
        "output_config": {"effort": config.EFFORT},
    }
    if system:
        kwargs["system"] = system
    return client.messages.create(**kwargs)


def run_turn(
    client: anthropic.Anthropic,
    system: str,
    conversation: list[dict[str, Any]],
) -> dict[str, Any]:
    """Drive the model until it stops calling tools, or until email confirmation
    is required.

    Returns one of:
      {"type": "text", "text": str, "conversation": [...]}
      {"type": "needs_confirmation", "email": {...}, "tool_use_id": str,
       "pending_tool_uses": [...], "conversation": [...]}
    """
    conversation = list(conversation)

    for _ in range(config.MAX_TOOL_ROUNDS):
        response = _call(client, system, conversation)

        if response.stop_reason == "refusal":
            return {
                "type": "text",
                "text": "I can't help with that request.",
                "conversation": conversation,
            }

        assistant_blocks = blocks_to_params(response.content)

        if response.stop_reason != "tool_use":
            return {
                "type": "text",
                "text": extract_text(response.content),
                "conversation": conversation + [{"role": "assistant", "content": assistant_blocks}],
            }

        conversation.append({"role": "assistant", "content": assistant_blocks})
        pending = tool_uses(assistant_blocks)

        email_call = next((b for b in pending if b["name"] == "send_email"), None)
        if email_call:
            # Park. Every tool_use in this assistant turn still needs a result,
            # so the whole set is carried forward for the resume step.
            return {
                "type": "needs_confirmation",
                "email": email_call["input"],
                "tool_use_id": email_call["id"],
                "pending_tool_uses": pending,
                "conversation": conversation,
            }

        conversation.append(
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": block["id"],
                        "content": f"Unknown tool: {block['name']}",
                        "is_error": True,
                    }
                    for block in pending
                ],
            }
        )

    return {
        "type": "text",
        "text": (
            "I stopped after too many tool steps without reaching an answer. "
            "Try rephrasing the request."
        ),
        "conversation": conversation,
    }


def resume_after_confirmation(
    client: anthropic.Anthropic,
    system: str,
    conversation: list[dict[str, Any]],
    pending_tool_uses: list[dict[str, Any]],
    confirmed_tool_use_id: str,
    result_text: str,
) -> dict[str, Any]:
    """Feed tool results back after the user confirms or cancels, then continue.

    Every parked tool_use gets a result, because the API rejects a turn where a
    tool_use has no matching tool_result.
    """
    results = []
    for block in pending_tool_uses:
        if block["id"] == confirmed_tool_use_id:
            results.append(
                {"type": "tool_result", "tool_use_id": block["id"], "content": result_text}
            )
        else:
            results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": block["id"],
                    "content": "Not executed.",
                    "is_error": True,
                }
            )
    conversation = list(conversation) + [{"role": "user", "content": results}]
    return run_turn(client, system, conversation)
