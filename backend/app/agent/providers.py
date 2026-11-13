"""OpenAI and Anthropic adapters. Tests inject a transport and never call the network."""

import json
import os
import urllib.error
import urllib.request
from collections.abc import Mapping
from typing import Protocol

from app.agent.errors import ConfigError, LLMError
from app.agent.types import Completion, ToolCall

OPENAI_URL = "https://api.openai.com/v1/chat/completions"
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
ANTHROPIC_MAX_TOKENS = 1024
DEFAULT_OPENAI_MODEL = "gpt-4.1-mini"
DEFAULT_ANTHROPIC_MODEL = "claude-sonnet-4-5"


class Transport(Protocol):
    def post_json(self, url: str, headers: dict[str, str], body: dict) -> dict: ...


class UrllibTransport:
    """Real HTTP POST. Unit tests pass a fake transport instead."""

    def post_json(self, url: str, headers: dict[str, str], body: dict) -> dict:
        request = urllib.request.Request(
            url,
            data=json.dumps(body).encode(),
            headers={**headers, "content-type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                raw = response.read().decode()
        except urllib.error.HTTPError as exc:
            exc.read()
            raise LLMError(f"model request failed: {exc.code}") from exc
        except urllib.error.URLError as exc:
            raise LLMError("model request failed") from exc
        return _json_object(raw)


class OpenAIClient:
    def __init__(self, *, api_key: str, model: str, transport: Transport | None = None):
        self.api_key = _required_value(api_key, "OPENAI_API_KEY")
        self.model = model
        self.transport = transport or UrllibTransport()

    def complete(self, messages: list[dict], tools: list[dict]) -> Completion:
        payload = self.transport.post_json(
            OPENAI_URL,
            {"Authorization": f"Bearer {self.api_key}"},
            openai_body(self.model, messages, tools),
        )
        return parse_openai_response(payload)


class AnthropicClient:
    def __init__(self, *, api_key: str, model: str, transport: Transport | None = None):
        self.api_key = _required_value(api_key, "ANTHROPIC_API_KEY")
        self.model = model
        self.transport = transport or UrllibTransport()

    def complete(self, messages: list[dict], tools: list[dict]) -> Completion:
        payload = self.transport.post_json(
            ANTHROPIC_URL,
            {"x-api-key": self.api_key, "anthropic-version": ANTHROPIC_VERSION},
            anthropic_body(self.model, messages, tools),
        )
        return parse_anthropic_response(payload)


class FakeLLM:
    """Scripted model for later tests. Replies are returned in order and never hit the network."""

    def __init__(self, replies: list[Completion]):
        self._replies = list(replies)
        self.calls: list[dict] = []

    def complete(self, messages: list[dict], tools: list[dict]) -> Completion:
        self.calls.append({"messages": messages, "tools": tools})
        if not self._replies:
            raise LLMError("fake model has no scripted reply")
        return self._replies.pop(0)


def build_client(env: Mapping[str, str] | None = None) -> OpenAIClient | AnthropicClient:
    values = os.environ if env is None else env
    provider = values.get("LLM_PROVIDER", "openai").strip().lower() or "openai"
    if provider == "openai":
        return OpenAIClient(
            api_key=_required(values, "OPENAI_API_KEY"),
            model=_optional(values, "OPENAI_MODEL", DEFAULT_OPENAI_MODEL),
        )
    if provider == "anthropic":
        return AnthropicClient(
            api_key=_required(values, "ANTHROPIC_API_KEY"),
            model=_optional(values, "ANTHROPIC_MODEL", DEFAULT_ANTHROPIC_MODEL),
        )
    raise ConfigError(f"unknown LLM provider: {provider}")


def openai_body(model: str, messages: list[dict], tools: list[dict]) -> dict:
    body = {"model": model, "messages": [_openai_message(message) for message in messages]}
    if tools:
        body["tools"] = [_openai_tool(tool) for tool in tools]
    return body


def anthropic_body(model: str, messages: list[dict], tools: list[dict]) -> dict:
    system, chat = _split_system(messages)
    body = {"model": model, "max_tokens": ANTHROPIC_MAX_TOKENS, "messages": chat}
    if system:
        body["system"] = system
    if tools:
        body["tools"] = [_anthropic_tool(tool) for tool in tools]
    return body


def parse_openai_response(payload: dict) -> Completion:
    try:
        message = payload["choices"][0]["message"]
    except (KeyError, IndexError, TypeError) as exc:
        raise LLMError("OpenAI response is missing a message") from exc
    if not isinstance(message, dict):
        raise LLMError("OpenAI response is missing a message")
    raw_calls = message.get("tool_calls") or []
    if not isinstance(raw_calls, list):
        raise LLMError("OpenAI tool call is malformed")
    text = _openai_text(message.get("content"))
    return Completion(text=text, tool_calls=tuple(_openai_call(raw) for raw in raw_calls))


def parse_anthropic_response(payload: dict) -> Completion:
    blocks = payload.get("content")
    if not isinstance(blocks, list):
        raise LLMError("Anthropic response is missing content")
    texts: list[str] = []
    calls: list[ToolCall] = []
    for block in blocks:
        if not isinstance(block, dict):
            raise LLMError("Anthropic response is missing content")
        if block.get("type") == "text":
            texts.append(str(block.get("text") or ""))
        elif block.get("type") == "tool_use":
            calls.append(_tool_call(block.get("id"), block.get("name"), block.get("input")))
    return Completion(text="".join(texts), tool_calls=tuple(calls))


def _openai_call(raw: dict) -> ToolCall:
    if not isinstance(raw, dict):
        raise LLMError("OpenAI tool call is malformed")
    function = raw.get("function")
    if not isinstance(function, dict):
        raise LLMError("OpenAI tool call is malformed")
    arguments = _load_arguments(function.get("arguments"))
    return _tool_call(raw.get("id"), function.get("name"), arguments)


def _tool_call(call_id: object, name: object, arguments: object) -> ToolCall:
    if not isinstance(call_id, str) or not call_id:
        raise LLMError("tool call is missing an id")
    if not isinstance(name, str) or not name:
        raise LLMError("tool call is missing a name")
    if not isinstance(arguments, dict):
        raise LLMError("tool call arguments are not valid JSON")
    if "reason" not in arguments:
        raise LLMError("tool call is missing a reason")
    reason = arguments["reason"]
    if not isinstance(reason, str) or not reason.strip():
        raise LLMError("tool call is missing a reason")
    rest = {key: value for key, value in arguments.items() if key != "reason"}
    return ToolCall(id=call_id, name=name, arguments=rest, reason=reason.strip())


def _load_arguments(raw: object) -> dict:
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str):
        raise LLMError("tool call arguments are not valid JSON")
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise LLMError("tool call arguments are not valid JSON") from exc
    if not isinstance(parsed, dict):
        raise LLMError("tool call arguments are not valid JSON")
    return parsed


def _openai_text(content: object) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                parts.append(str(item.get("text") or ""))
            else:
                parts.append(str(item))
        return "".join(parts)
    raise LLMError("OpenAI response is missing a message")


def _openai_message(message: dict) -> dict:
    if message.get("role") == "tool":
        content = message.get("content")
        tool_call_id = message.get("tool_call_id")
        if not isinstance(content, str) or not isinstance(tool_call_id, str) or not tool_call_id:
            raise LLMError("message must have a role and text content")
        return {"role": "tool", "tool_call_id": tool_call_id, "content": content}
    calls = message.get("tool_calls")
    if calls:
        if message.get("role") != "assistant" or not isinstance(calls, list):
            raise LLMError("message must have a role and text content")
        content = message.get("content")
        if content is not None and not isinstance(content, str):
            raise LLMError("message must have a role and text content")
        return {
            "role": "assistant",
            "content": content or None,
            "tool_calls": [_openai_outgoing_call(call) for call in calls],
        }
    return _text_message(message)


def _openai_outgoing_call(call: dict) -> dict:
    name = call.get("name")
    call_id = call.get("id")
    arguments = call.get("arguments")
    reason = call.get("reason")
    if not isinstance(name, str) or not isinstance(call_id, str) or not isinstance(arguments, dict):
        raise LLMError("message must have a role and text content")
    if not isinstance(reason, str):
        raise LLMError("message must have a role and text content")
    return {
        "id": call_id,
        "type": "function",
        "function": {
            "name": name,
            "arguments": json.dumps({**arguments, "reason": reason}),
        },
    }


def _text_message(message: dict) -> dict:
    role = message.get("role")
    content = message.get("content")
    if role not in {"system", "user", "assistant"} or not isinstance(content, str):
        raise LLMError("message must have a role and text content")
    return {"role": role, "content": content}


def _split_system(messages: list[dict]) -> tuple[str, list[dict]]:
    system: list[str] = []
    chat: list[dict] = []
    pending: list[dict] = []

    def flush_results() -> None:
        if pending:
            chat.append({"role": "user", "content": list(pending)})
            pending.clear()

    for message in messages:
        if message.get("role") == "tool":
            content = message.get("content")
            tool_call_id = message.get("tool_call_id")
            if not isinstance(content, str) or not isinstance(tool_call_id, str) or not tool_call_id:
                raise LLMError("message must have a role and text content")
            pending.append({"type": "tool_result", "tool_use_id": tool_call_id, "content": content})
            continue
        flush_results()
        calls = message.get("tool_calls")
        if calls:
            if message.get("role") != "assistant" or not isinstance(calls, list):
                raise LLMError("message must have a role and text content")
            blocks: list[dict] = []
            text = message.get("content") or ""
            if not isinstance(text, str):
                raise LLMError("message must have a role and text content")
            if text:
                blocks.append({"type": "text", "text": text})
            for call in calls:
                blocks.append(_anthropic_outgoing_call(call))
            chat.append({"role": "assistant", "content": blocks})
            continue
        copied = _text_message(message)
        if copied["role"] == "system":
            system.append(copied["content"])
        else:
            chat.append(copied)
    flush_results()
    return "\n\n".join(system), chat


def _anthropic_outgoing_call(call: dict) -> dict:
    name = call.get("name")
    call_id = call.get("id")
    arguments = call.get("arguments")
    reason = call.get("reason")
    if not isinstance(name, str) or not isinstance(call_id, str) or not isinstance(arguments, dict):
        raise LLMError("message must have a role and text content")
    if not isinstance(reason, str):
        raise LLMError("message must have a role and text content")
    return {"type": "tool_use", "id": call_id, "name": name, "input": {**arguments, "reason": reason}}


def _openai_tool(tool: dict) -> dict:
    name, description, parameters = _tool_spec(tool)
    return {
        "type": "function",
        "function": {"name": name, "description": description, "parameters": parameters},
    }


def _anthropic_tool(tool: dict) -> dict:
    name, description, parameters = _tool_spec(tool)
    return {"name": name, "description": description, "input_schema": parameters}


def _tool_spec(tool: dict) -> tuple[str, str, dict]:
    name = tool.get("name")
    description = tool.get("description")
    parameters = tool.get("parameters")
    if not isinstance(name, str) or not name:
        raise LLMError("tool spec is missing a name")
    if not isinstance(description, str):
        raise LLMError("tool spec is missing a description")
    if not isinstance(parameters, dict):
        raise LLMError("tool spec is missing parameters")
    return name, description, parameters


def _required(env: Mapping[str, str], name: str) -> str:
    return _required_value(env.get(name, ""), name)


def _required_value(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{name} is not set")
    return value.strip()


def _optional(env: Mapping[str, str], name: str, default: str) -> str:
    value = env.get(name, "").strip()
    return value or default


def _json_object(raw: str) -> dict:
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise LLMError("model response was not JSON") from exc
    if not isinstance(parsed, dict):
        raise LLMError("model response was not JSON")
    return parsed
