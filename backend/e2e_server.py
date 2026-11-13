"""Seeded desk with a scripted model for browser tests. Pytest does not collect this file."""

import json

import uvicorn

from app.agent.types import Completion, ToolCall
from app.db import create_session_factory, create_sqlite_engine, init_db
from app.main import create_app
from app.seed import seed

_FALLBACK = "I can take a booking, a quote, or a note for Maria."


class DeskLLM:
    """Follows the demo script. It never calls a model API."""

    def complete(self, messages: list[dict], tools: list[dict]) -> Completion:
        del tools
        last = messages[-1]
        if last.get("role") == "tool":
            return self._after(messages)
        text = last.get("content") if isinstance(last.get("content"), str) else ""
        lowered = text.lower()
        if "gas" in lowered:
            return _call(
                "escalate",
                {"detail": "Customer smells gas and is still in the house."},
                "Gas smell",
            )
        if "sunday" in lowered:
            return Completion(
                "",
                (
                    ToolCall(
                        "call-setting",
                        "update_setting",
                        {"key": "sunday_calls", "value": False},
                        "Owner said Sunday calls are over",
                    ),
                    ToolCall(
                        "call-policy",
                        "remember_policy",
                        {"content": "We stopped doing Sunday calls"},
                        "Owner said Sunday calls are over",
                    ),
                ),
            )
        if "friday" in lowered and "quote" in lowered:
            return _call(
                "create_automation",
                {"instruction": text, "weekday": "friday"},
                "Owner asked for a weekly quote report",
            )
        if "leak" in lowered or "book" in lowered:
            return _call(
                "check_availability",
                {"service": "leak", "day": "2026-11-13"},
                "James asked to book the leak visit",
            )
        return Completion(_FALLBACK, ())

    def _after(self, messages: list[dict]) -> Completion:
        payloads = [_payload(message) for message in messages if message.get("role") == "tool"]
        if any("slots" in item for item in payloads) and not any(item.get("status") == "scheduled" for item in payloads):
            slots = next(item["slots"] for item in payloads if "slots" in item)
            slot = next(
                (item for item in slots if str(item.get("start", "")).startswith("2026-11-13T10:00")),
                None,
            )
            if slot is None:
                raise RuntimeError("no 10:00 leak slot")
            return _call(
                "book_appointment",
                {
                    "service": "leak",
                    "start": slot["start"],
                    "summary": "Leak under the kitchen sink",
                    "technician_id": slot["technician_id"],
                    "price": 129,
                },
                "James asked to book the leak visit",
            )
        for item in reversed(payloads):
            if item.get("status") == "scheduled":
                return Completion(
                    "You're booked Friday at 10:00 AM. I have 1842 N 92nd St and gate code 4481.",
                    (),
                )
            if "automation_id" in item:
                return Completion("I'll report open quotes every Friday at 7:30.", ())
            if "escalation_id" in item:
                return Completion("I'm handing this to Maria. Please leave the house.", ())
        return Completion("Sunday calls are off. I'll remember that.", ())


def _call(name: str, arguments: dict, reason: str) -> Completion:
    return Completion("", (ToolCall("call-1", name, arguments, reason),))


def _payload(message: dict) -> dict:
    content = message.get("content")
    if not isinstance(content, str):
        return {}
    parsed = json.loads(content)
    return parsed if isinstance(parsed, dict) else {}


def main() -> None:
    engine = create_sqlite_engine("sqlite://")
    init_db(engine)
    factory = create_session_factory(engine)
    with factory() as session:
        seed(session)
    uvicorn.run(create_app(factory, llm=DeskLLM()), host="127.0.0.1", port=8000, log_level="warning")


if __name__ == "__main__":
    main()
