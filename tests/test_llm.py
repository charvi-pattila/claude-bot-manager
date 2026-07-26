"""Tool-loop behaviour. No network: the Anthropic client is a stub."""

import json

import config
import llm


class Block:
    def __init__(self, **fields):
        self.__dict__.update(fields)


def text(value):
    return Block(type="text", text=value)


def tool_use(block_id, name, payload):
    return Block(type="tool_use", id=block_id, name=name, input=payload)


class FakeResponse:
    def __init__(self, content, stop_reason):
        self.content = content
        self.stop_reason = stop_reason


class FakeMessages:
    def __init__(self, responses, calls):
        self._responses = list(responses)
        self._calls = calls

    def create(self, **kwargs):
        self._calls.append(kwargs)
        return self._responses.pop(0)


class FakeClient:
    def __init__(self, responses):
        self.calls = []
        self.messages = FakeMessages(responses, self.calls)


def test_extract_text_joins_all_text_blocks():
    content = [
        Block(type="thinking", thinking="internal", signature="sig"),
        text("first"),
        text("second"),
    ]
    # The old code read content[0].text, which on current models is often a
    # thinking block rather than the answer.
    assert llm.extract_text(content) == "first\nsecond"


def test_plain_reply_returns_text():
    client = FakeClient([FakeResponse([text("Hello there")], "end_turn")])
    result = llm.run_turn(client, "system", [{"role": "user", "content": "hi"}])
    assert result["type"] == "text"
    assert result["text"] == "Hello there"


def test_send_email_parks_for_confirmation():
    """The model asking to send mail must never send mail on its own."""
    email = {"to": "a@example.com", "subject": "Hi", "body": "Body"}
    client = FakeClient(
        [FakeResponse([text("Sending now"), tool_use("t1", "send_email", email)], "tool_use")]
    )
    result = llm.run_turn(client, "system", [{"role": "user", "content": "email a@example.com"}])

    assert result["type"] == "needs_confirmation"
    assert result["email"] == email
    assert result["tool_use_id"] == "t1"


def test_loop_runs_multiple_rounds():
    """Regression: the loop used to stop after one round of tool use, silently
    dropping any follow-up call the model wanted to make."""
    client = FakeClient(
        [
            FakeResponse([tool_use("t1", "mystery_tool", {})], "tool_use"),
            FakeResponse([text("Done")], "end_turn"),
        ]
    )
    result = llm.run_turn(client, "system", [{"role": "user", "content": "go"}])

    assert result["type"] == "text"
    assert result["text"] == "Done"
    assert len(client.calls) == 2


def test_loop_is_bounded():
    responses = [
        FakeResponse([tool_use(f"t{i}", "mystery_tool", {})], "tool_use")
        for i in range(config.MAX_TOOL_ROUNDS + 2)
    ]
    client = FakeClient(responses)
    result = llm.run_turn(client, "system", [{"role": "user", "content": "loop"}])

    assert result["type"] == "text"
    assert len(client.calls) == config.MAX_TOOL_ROUNDS


def test_refusal_is_handled():
    client = FakeClient([FakeResponse([], "refusal")])
    result = llm.run_turn(client, "system", [{"role": "user", "content": "..."}])
    assert result["type"] == "text"


def test_conversation_state_is_json_serializable():
    """Parked turns are persisted to the database, so the carried state has to
    survive a round trip through JSON."""
    email = {"to": "a@example.com", "subject": "S", "body": "B"}
    client = FakeClient([FakeResponse([tool_use("t1", "send_email", email)], "tool_use")])
    result = llm.run_turn(client, "system", [{"role": "user", "content": "hi"}])

    restored = json.loads(json.dumps(result["conversation"]))
    assert restored == result["conversation"]


def test_resume_answers_every_parked_tool_use():
    """The API rejects a turn where any tool_use lacks a matching tool_result."""
    pending = [
        {"type": "tool_use", "id": "t1", "name": "send_email", "input": {}},
        {"type": "tool_use", "id": "t2", "name": "other_tool", "input": {}},
    ]
    client = FakeClient([FakeResponse([text("Sent.")], "end_turn")])
    llm.resume_after_confirmation(client, "system", [], pending, "t1", "Email sent.")

    tool_results = client.calls[0]["messages"][-1]["content"]
    assert {r["tool_use_id"] for r in tool_results} == {"t1", "t2"}
    assert tool_results[0]["content"] == "Email sent."
    assert tool_results[1]["is_error"] is True


def test_declined_result_is_fed_back():
    client = FakeClient([FakeResponse([text("Understood.")], "end_turn")])
    pending = [{"type": "tool_use", "id": "t1", "name": "send_email", "input": {}}]
    llm.resume_after_confirmation(
        client, "system", [], pending, "t1", "The user declined to send this email."
    )
    result = client.calls[0]["messages"][-1]["content"][0]
    assert "declined" in result["content"]
