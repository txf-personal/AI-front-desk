"""Provider adapters, parsed from saved payloads. No network calls."""

import json

import pytest

from app.agent.errors import ConfigError, LLMError
from app.agent.providers import (
    ANTHROPIC_URL,
    ANTHROPIC_VERSION,
    DEFAULT_ANTHROPIC_MODEL,
    DEFAULT_OPENAI_MODEL,
    OPENAI_URL,
    AnthropicClient,
    FakeLLM,
    OpenAIClient,
    build_client,
    parse_anthropic_response,
    parse_openai_response,
)
from app.agent.types import Completion, ToolCall

BOOK_ARGUMENTS = {"service": "leak", "start": "2026-11-13T13:00:00-08:00"}
BOOK_REASON = "Customer asked to book the leak visit"
REMEMBER_ARGUMENTS = {"content": "Gate code 4481", "kind": "access"}
REMEMBER_REASON = "Customer gave a gate code to keep"
TOOLS = [
    {
        "name": "book_appointment",
        "description": "Book a visit",
        "parameters": {"type": "object", "properties": {"service": {"type": "string"}}},
    }
]
MESSAGES = [
    {"role": "system", "content": "You are the front desk."},
    {"role": "user", "content": "My water heater is leaking."},
]


def _with_reason(arguments: dict, reason: str) -> dict:
    return {**arguments, "reason": reason}


OPENAI_PAYLOAD = {
    "choices": [
        {
            "message": {
                "role": "assistant",
                "content": "I'll book that.",
                "tool_calls": [
                    {
                        "id": "call_book",
                        "type": "function",
                        "function": {
                            "name": "book_appointment",
                            "arguments": json.dumps(_with_reason(BOOK_ARGUMENTS, BOOK_REASON)),
                        },
                    },
                    {
                        "id": "call_remember",
                        "type": "function",
                        "function": {
                            "name": "remember_customer_fact",
                            "arguments": json.dumps(_with_reason(REMEMBER_ARGUMENTS, REMEMBER_REASON)),
                        },
                    },
                ],
            }
        }
    ]
}

ANTHROPIC_PAYLOAD = {
    "content": [
        {"type": "text", "text": "I'll book that."},
        {
            "type": "tool_use",
            "id": "toolu_book",
            "name": "book_appointment",
            "input": _with_reason(BOOK_ARGUMENTS, BOOK_REASON),
        },
        {
            "type": "tool_use",
            "id": "toolu_remember",
            "name": "remember_customer_fact",
            "input": _with_reason(REMEMBER_ARGUMENTS, REMEMBER_REASON),
        },
    ]
}


def _core(calls: tuple[ToolCall, ...]) -> list[tuple[str, dict, str]]:
    return [(call.name, call.arguments, call.reason) for call in calls]


class FakeTransport:
    def __init__(self, response: dict):
        self.response = response
        self.requests: list[dict] = []

    def post_json(self, url: str, headers: dict[str, str], body: dict) -> dict:
        self.requests.append({"url": url, "headers": headers, "body": body})
        return self.response


def test_openai_and_anthropic_payloads_parse_to_the_same_calls():
    openai = parse_openai_response(OPENAI_PAYLOAD)
    anthropic = parse_anthropic_response(ANTHROPIC_PAYLOAD)
    expected = [
        ("book_appointment", BOOK_ARGUMENTS, BOOK_REASON),
        ("remember_customer_fact", REMEMBER_ARGUMENTS, REMEMBER_REASON),
    ]
    assert _core(openai.tool_calls) == expected
    assert _core(anthropic.tool_calls) == expected
    assert openai.text == "I'll book that."
    assert anthropic.text == "I'll book that."
    assert [call.id for call in openai.tool_calls] == ["call_book", "call_remember"]
    assert [call.id for call in anthropic.tool_calls] == ["toolu_book", "toolu_remember"]
    assert "reason" not in openai.tool_calls[0].arguments


def test_openai_accepts_arguments_that_are_already_objects():
    payload = {
        "choices": [
            {
                "message": {
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "function": {
                                "name": "book_appointment",
                                "arguments": _with_reason(BOOK_ARGUMENTS, BOOK_REASON),
                            },
                        }
                    ],
                }
            }
        ]
    }
    parsed = parse_openai_response(payload)
    assert parsed.text == ""
    assert _core(parsed.tool_calls) == [("book_appointment", BOOK_ARGUMENTS, BOOK_REASON)]


def test_text_only_reply_has_no_tool_calls():
    parsed = parse_openai_response({"choices": [{"message": {"content": "Thursday works."}}]})
    assert parsed == Completion(text="Thursday works.", tool_calls=())


@pytest.mark.parametrize(
    "arguments",
    [
        json.dumps(BOOK_ARGUMENTS),
        json.dumps(_with_reason(BOOK_ARGUMENTS, "  ")),
        "{not json",
        json.dumps(["not", "an", "object"]),
    ],
)
def test_openai_rejects_a_tool_call_without_a_usable_reason(arguments):
    payload = {
        "choices": [
            {
                "message": {
                    "content": None,
                    "tool_calls": [
                        {"id": "call_1", "function": {"name": "book_appointment", "arguments": arguments}}
                    ],
                }
            }
        ]
    }
    with pytest.raises(LLMError, match="tool call"):
        parse_openai_response(payload)


def test_openai_rejects_a_message_that_is_not_an_object():
    with pytest.raises(LLMError, match="missing a message"):
        parse_openai_response({"choices": [{"message": None}]})


def test_openai_rejects_tool_calls_that_are_not_a_list():
    with pytest.raises(LLMError, match="malformed"):
        parse_openai_response({"choices": [{"message": {"content": None, "tool_calls": "nope"}}]})


def test_anthropic_rejects_a_tool_call_without_a_reason():
    payload = {
        "content": [
            {"type": "tool_use", "id": "toolu_1", "name": "book_appointment", "input": BOOK_ARGUMENTS}
        ]
    }
    with pytest.raises(LLMError, match="missing a reason"):
        parse_anthropic_response(payload)


def test_openai_client_sends_the_key_in_the_header_and_parses_the_fixture():
    transport = FakeTransport(OPENAI_PAYLOAD)
    client = OpenAIClient(api_key="sk-test", model="gpt-test", transport=transport)
    parsed = client.complete(MESSAGES, TOOLS)

    assert _core(parsed.tool_calls) == _core(parse_openai_response(OPENAI_PAYLOAD).tool_calls)
    request = transport.requests[0]
    assert request["url"] == OPENAI_URL
    assert request["headers"]["Authorization"] == "Bearer sk-test"
    assert "sk-test" not in json.dumps(request["body"])
    assert request["body"]["model"] == "gpt-test"
    assert request["body"]["messages"] == MESSAGES
    assert request["body"]["tools"][0]["type"] == "function"
    assert request["body"]["tools"][0]["function"]["name"] == "book_appointment"


def test_anthropic_client_splits_system_text_and_keeps_the_key_out_of_the_body():
    transport = FakeTransport(ANTHROPIC_PAYLOAD)
    client = AnthropicClient(api_key="sk-ant-test", model="claude-test", transport=transport)
    parsed = client.complete(MESSAGES, TOOLS)

    assert parsed.text == "I'll book that."
    request = transport.requests[0]
    assert request["url"] == ANTHROPIC_URL
    assert request["headers"]["x-api-key"] == "sk-ant-test"
    assert request["headers"]["anthropic-version"] == ANTHROPIC_VERSION
    assert "sk-ant-test" not in json.dumps(request["body"])
    assert request["body"]["model"] == "claude-test"
    assert request["body"]["system"] == "You are the front desk."
    assert request["body"]["messages"] == [{"role": "user", "content": "My water heater is leaking."}]
    assert request["body"]["tools"][0]["input_schema"] == TOOLS[0]["parameters"]


def test_factory_requires_the_selected_provider_key():
    with pytest.raises(ConfigError, match="OPENAI_API_KEY is not set"):
        build_client({})
    with pytest.raises(ConfigError, match="OPENAI_API_KEY is not set"):
        build_client({"LLM_PROVIDER": "openai", "OPENAI_API_KEY": "   "})
    with pytest.raises(ConfigError, match="ANTHROPIC_API_KEY is not set"):
        build_client({"LLM_PROVIDER": "anthropic", "OPENAI_API_KEY": "sk-test"})


def test_factory_uses_the_configured_model_and_defaults_to_openai():
    openai = build_client({"OPENAI_API_KEY": " sk-test "})
    assert isinstance(openai, OpenAIClient)
    assert openai.api_key == "sk-test"
    assert openai.model == DEFAULT_OPENAI_MODEL

    chosen = build_client({"LLM_PROVIDER": "OpenAI", "OPENAI_API_KEY": "sk-test", "OPENAI_MODEL": "gpt-test"})
    assert chosen.model == "gpt-test"

    anthropic = build_client({"LLM_PROVIDER": "anthropic", "ANTHROPIC_API_KEY": "sk-ant-test"})
    assert isinstance(anthropic, AnthropicClient)
    assert anthropic.model == DEFAULT_ANTHROPIC_MODEL


def test_factory_rejects_an_unknown_provider():
    with pytest.raises(ConfigError, match="unknown LLM provider: gemini"):
        build_client({"LLM_PROVIDER": "gemini", "OPENAI_API_KEY": "sk-test"})


def test_fake_model_returns_scripted_replies_in_order():
    first = Completion(text="One", tool_calls=())
    second = Completion(
        text="",
        tool_calls=(ToolCall(id="1", name="book_appointment", arguments=BOOK_ARGUMENTS, reason=BOOK_REASON),),
    )
    fake = FakeLLM([first, second])
    assert fake.complete(MESSAGES, TOOLS) is first
    assert fake.complete(MESSAGES, []) is second
    assert fake.calls[0]["tools"] == TOOLS
    assert fake.calls[1]["tools"] == []
    with pytest.raises(LLMError, match="no scripted reply"):
        fake.complete(MESSAGES, [])
