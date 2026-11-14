"""One shop story through the HTTP API, plus the process config the server reads."""

import json

from fastapi.testclient import TestClient

from app.agent.providers import AnthropicClient, build_client
from app.agent.types import Completion, ToolCall
from app.main import create_app
from app.seed import seed
from app.serve import create_runtime_app

JAMES = "James Okonkwo"
BOOK_REASON = "James asked to book the leak visit"
SUNDAY_REASON = "Owner said Sunday calls are over"
FRIDAY_WORDS = "Every Friday, tell me who is still waiting on a quote."
REPORT_REASON = "Owner asked for a weekly quote report"
GAS_DETAIL = "Customer smells gas and is still in the house."
LUIS_REMINDER = (
    "Luis Alvarez, reminder from Northside Plumbing: Brooke Hale is scheduled "
    "Friday, November 13 at 9:00 AM for Main bathroom drain."
)
SAM_CHASE = "Sam Rivera, your Drain cleaning quote for $149 is still waiting. Kitchen sink draining slowly."


class StoryModel:
    """Scripted front desk for the demo. It never calls a provider."""

    def complete(self, messages: list[dict], tools: list[dict]) -> Completion:
        del tools
        last = messages[-1]
        if last.get("role") == "tool":
            return self._after(messages)
        text = last.get("content") if isinstance(last.get("content"), str) else ""
        lowered = text.lower()
        if "gas" in lowered:
            return _one("escalate", {"detail": GAS_DETAIL}, "Gas smell")
        if "sunday" in lowered:
            return Completion(
                "",
                (
                    ToolCall("call-setting", "update_setting", {"key": "sunday_calls", "value": False}, SUNDAY_REASON),
                    ToolCall("call-policy", "remember_policy", {"content": "We stopped doing Sunday calls"}, SUNDAY_REASON),
                ),
            )
        if "friday" in lowered and "quote" in lowered:
            return _one("create_automation", {"instruction": text, "weekday": "friday"}, REPORT_REASON)
        if "leak" in lowered or "book" in lowered:
            return _one(
                "check_availability",
                {"service": "leak", "day": "2026-11-13"},
                BOOK_REASON,
            )
        raise AssertionError(f"unexpected message: {text}")

    def _after(self, messages: list[dict]) -> Completion:
        payloads = _turn_payloads(messages)
        if any("slots" in item for item in payloads) and not any(item.get("status") == "scheduled" for item in payloads):
            slots = next(item["slots"] for item in payloads if "slots" in item)
            slot = next(item for item in slots if str(item.get("start", "")).startswith("2026-11-13T10:00"))
            return _one(
                "book_appointment",
                {
                    "service": "leak",
                    "start": slot["start"],
                    "summary": "Leak under the kitchen sink",
                    "technician_id": slot["technician_id"],
                    "price": 129,
                },
                BOOK_REASON,
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


def test_demo_story_books_james_then_runs_the_morning_jobs(session_factory, session):
    seed(session)
    client = TestClient(create_app(session_factory, llm=StoryModel()))
    shop = client.get("/api/bootstrap").json()
    james = next(customer for customer in shop["customers"] if customer["name"] == JAMES)

    booked = client.post(
        "/api/messages",
        json={"audience": "customer", "customer_id": james["id"], "body": "Please book the leak for Friday at 10."},
    )
    assert booked.status_code == 200
    assert "gate code 4481" in booked.json()["reply"]
    assert "1842 N 92nd St" in booked.json()["reply"]

    sunday = client.post("/api/messages", json={"audience": "owner", "body": "We stopped doing Sunday calls"})
    assert sunday.status_code == 200
    assert sunday.json()["reply"] == "Sunday calls are off. I'll remember that."
    friday = client.post("/api/messages", json={"audience": "owner", "body": FRIDAY_WORDS})
    assert friday.status_code == 200
    gas = client.post(
        "/api/messages",
        json={"audience": "customer", "customer_id": james["id"], "body": "I smell gas and I am still in the house."},
    )
    assert gas.status_code == 200

    shop = client.get("/api/bootstrap").json()
    assert shop["settings"]["sunday_calls"] is False
    assert "We stopped doing Sunday calls" in [memory["content"] for memory in shop["shop_memories"]]
    james_now = next(customer for customer in shop["customers"] if customer["id"] == james["id"])
    assert "We stopped doing Sunday calls" not in [memory["content"] for memory in james_now["memories"]]
    leak = [job for job in shop["appointments"] if job["customer_id"] == james["id"] and job["service"] == "leak"]
    assert len(leak) == 1
    assert leak[0]["status"] == "scheduled"
    assert leak[0]["address"] == "1842 N 92nd St, Seattle, WA 98115"
    assert leak[0]["access_notes"] == "Gate code 4481"
    report = next(job for job in shop["automations"] if job["name"] == "Quotes still waiting")
    assert report["schedule"] == {"weekday": "friday", "hour": 7, "minute": 30}
    opened = next(item for item in shop["escalations"] if item["reason"] == "Gas smell")
    assert opened["status"] == "open"
    assert opened["customer_id"] == james["id"]
    assert opened["detail"] == GAS_DETAIL

    morning = client.post("/api/clock/advance", json={"to": "next_morning"})
    assert morning.status_code == 200
    assert morning.json()["clock"] == "2026-11-13T07:00:00-08:00"
    texts = _bodies(client.get("/api/bootstrap").json())
    assert LUIS_REMINDER in texts
    assert SAM_CHASE in texts
    assert any("morning rundown for Friday, November 13" in text and "Luis Alvarez" in text and JAMES in text for text in texts)
    assert not any("quotes still waiting on Friday" in text for text in texts)

    later = client.post("/api/clock/advance", json={"hours": 4})
    assert later.status_code == 200
    assert later.json()["clock"] == "2026-11-13T11:00:00-08:00"
    shop = client.get("/api/bootstrap").json()
    report_text = next(text for text in _bodies(shop) if "quotes still waiting on Friday" in text)
    assert "Sam Rivera" in report_text
    assert "Luis Alvarez" not in report_text
    reasons = {(row["action"], row["reasoning"]) for row in shop["audits"]}
    assert ("book_appointment", BOOK_REASON) in reasons
    assert ("set_setting", SUNDAY_REASON) in reasons
    assert ("remember", SUNDAY_REASON) in reasons
    assert ("create_automation", REPORT_REASON) in reasons
    assert ("open_escalation", "Gas smell") in reasons
    assert ("morning_rundown", "Morning rundown lists visits scheduled on 2026-11-13.") in reasons
    assert ("appointment_reminder", "Day-before reminder lists visits scheduled on 2026-11-13.") in reasons
    assert ("quote_followup", "Quote has had no customer reply for 2 days.") in reasons
    assert ("quote_report", "Weekly report lists quotes that are still waiting.") in reasons


def test_runtime_seeds_once_and_serves_the_desk(tmp_path, monkeypatch):
    desk = tmp_path / "dist"
    desk.mkdir()
    (desk / "index.html").write_text("<title>Northside Plumbing</title>", encoding="utf-8")
    monkeypatch.setenv("DESK_DIST", str(desk))
    monkeypatch.setenv("NORTHSIDE_DB", str(tmp_path / "shop.db"))

    app = create_runtime_app()
    with TestClient(app) as client:
        page = client.get("/")
        shop = client.get("/api/bootstrap")
    assert page.status_code == 200
    assert "Northside Plumbing" in page.text
    assert shop.status_code == 200
    assert shop.json()["business"]["name"] == "Northside Plumbing"
    assert len(shop.json()["customers"]) == 5

    again = create_runtime_app()
    with TestClient(again) as client:
        repeat = client.get("/api/bootstrap").json()
    assert len(repeat["customers"]) == 5
    assert repeat["clock"] == shop.json()["clock"]


def test_client_reads_the_provider_and_key_from_the_environment(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_MODEL", raising=False)
    monkeypatch.delenv("ANTHROPIC_MODEL", raising=False)
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", " sk-ant-from-env ")
    monkeypatch.setenv("ANTHROPIC_MODEL", "claude-from-env")

    client = build_client()

    assert isinstance(client, AnthropicClient)
    assert client.api_key == "sk-ant-from-env"
    assert client.model == "claude-from-env"


def _one(name: str, arguments: dict, reason: str) -> Completion:
    return Completion("", (ToolCall("call-1", name, arguments, reason),))


def _turn_payloads(messages: list[dict]) -> list[dict]:
    payloads = []
    for message in reversed(messages):
        role = message.get("role")
        if role == "user":
            break
        if role == "tool":
            payloads.append(_payload(message))
    payloads.reverse()
    return payloads


def _payload(message: dict) -> dict:
    content = message.get("content")
    if not isinstance(content, str):
        return {}
    parsed = json.loads(content)
    return parsed if isinstance(parsed, dict) else {}


def _bodies(shop: dict) -> list[str]:
    return [message["body"] for conversation in shop["conversations"] for message in conversation["messages"]]
