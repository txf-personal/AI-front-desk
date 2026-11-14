# Northside Plumbing

An AI front desk for one plumbing shop. It books visits, remembers customers, chases quiet quotes, and hands emergencies to the owner. The clock is simulated. Messages stay in the database.

## Run it

Copy the environment file and put your key in it. The key is the only paid service.

```sh
cp .env.example .env
```

`LLM_PROVIDER` is `openai` or `anthropic`. Set the matching key: `OPENAI_API_KEY` or `ANTHROPIC_API_KEY`. Model names are optional.

On a laptop:

```sh
./start.sh
```

That creates a Python 3.12 virtualenv, installs the API, builds the desk, seeds the shop the first time, and serves http://127.0.0.1:8000. The shop file is `data/northside.db`. Starting again does not rewind the clock or duplicate the sample week.

In a container:

```sh
docker compose up --build
```

Compose reads the same `.env`. The database lives in a volume, so a restart keeps the clock.

You can open the seeded shop with no key. Sending a message needs one. The morning jobs do not: they use fixed templates.

## Click-through

The clock starts Thursday, November 12, 2026, at 4:00 PM Pacific. James Okonkwo is selected. His gate code is on the thread.

1. As James, ask to book a leak visit for Friday morning. The visit should keep 1842 N 92nd St and gate code 4481. Activity shows `book_appointment` and the reason the desk gave.
2. As the owner, say "We stopped doing Sunday calls." The Sunday pill turns off. The sentence shows under Policies and does not show on James.
3. As the owner, say "Every Friday, tell me who is still waiting on a quote." A job named Quotes still waiting appears for Friday at 7:30.
4. As James, say you smell gas and are still in the house. An open escalation names him. Resolve it. The leak visit from step 1 is still the only new booking.
5. Press Next morning. The clock is Friday at 7:00 AM. Luis Alvarez gets a reminder for 9:00 AM with Brooke Hale. Sam Rivera gets a chase for the $149 drain quote. Maria Chen gets a rundown that names Luis and James. The Friday report has not been sent.
6. Press Four hours. The owner thread gets the quotes-still-waiting report. Sam is on it. Luis is not, because that drain is already booked.

Chat replies depend on the model. The reminder, rundown, chase, and Friday report do not: those sentences are templates, and each one has an activity row that says why it was sent.

## Why this shape

SQLite holds the shop, the memories, and the clock, so the demo is one process. The model picks a tool and drafts the words. The shop checks hours, the price book, and who is allowed to call the tool, then writes the row and the reason. OpenAI and Anthropic sit behind one adapter. The key goes in a header, not the prompt. The desk is a React page that reloads the shop after every send and every clock jump.

## Tests

```sh
cd backend && .venv/bin/pytest
cd frontend && npm test
cd frontend && npm run e2e
```

None of them call a model API. Provider tests use saved payloads. The agent and the morning jobs use a scripted model. The browser suite runs the click-through against that same kind of script.
