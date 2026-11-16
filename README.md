# Northside Plumbing

An AI front desk for one plumbing shop. It books visits, remembers customers, chases quiet quotes, and hands emergencies to the owner. The clock is simulated. Messages stay in the database.

## High-Level Architecture

One Python process opens SQLite, seeds the shop when the file is empty, and serves the built desk beside the API. The desk reloads `GET /api/bootstrap` after every action. A message may call the model. Moving the clock does not.

```mermaid
flowchart TB
  Browser["Browser"] --> Desk

  subgraph Desk ["Built desk"]
    direction LR
    Threads["Customer thread and owner thread"]
    ShopView["Schedule, policies, automations, activity, escalations"]
    ClockFace["Shop clock"]
  end

  Desk --> API

  subgraph API ["FastAPI"]
    direction LR
    Boot["GET /api/bootstrap"]
    Msg["POST /api/messages"]
    Adv["POST /api/clock/advance"]
    Rest["Customers and resolve escalation"]
  end

  Boot --> Catchup["Catch up stored sentences, book a named visit, finish past visits"]
  Msg --> Save["Save the message"]
  Adv --> Due["Crossed times only"]
  Rest --> Rules

  Save --> Route{"Known owner sentence?"}
  Route -->|Open or close a day, add a zip, save or remove a reminder| Direct["Write the shop change"]
  Route -->|Any other message| Pack["Context pack"]

  Pack --> Model["OpenAI or Anthropic"]
  Model --> Call{"Tool call?"}
  Call -->|Yes, and the audience allows it| Guard["Time, technician, past time, gas, service area"]
  Guard --> Model
  Call -->|Reply| Check["Hours, claimed booking, today or tomorrow"]

  Direct --> Rules
  Guard --> Rules
  Check --> Rules
  Catchup --> Rules
  Due --> Jobs
  Due --> Rules

  subgraph Jobs ["Due jobs, fixed text"]
    direction LR
    Rundown["Morning rundown"]
    Reminder["Visit reminder"]
    Chase["Quote chase"]
    Report["Friday quote report"]
  end

  Jobs --> Rules

  subgraph Rules ["Shop rules"]
    direction LR
    Book["Booking and quotes"]
    Hours["Hours and service area"]
    Mem["Customer and shop memory"]
    People["Customers"]
    Esc["Escalations"]
    Aud["Audit log"]
  end

  Rules --> DB[("SQLite")]
```

The context pack is the only shop state the model sees. It includes the clock, hours, closed days, service area, technicians, and shop memories. A customer pack also includes that person, their memories, open jobs, and recent messages. An owner pack includes open quotes, the schedule, and the owner thread.

A customer tool may remember a fact, set an address, check openings, book, quote, or escalate. An owner tool may change a setting, remember a policy, or create an automation. The customer id on a tool call comes from the thread, not from the model. A visit is booked only after the customer names a time and one technician. A start before the clock is refused, a booking that mentions gas is refused, and an address outside the service area is not booked.

Bootstrap applies owner sentences that were already stored: daily reminders, reminder removals, and service-area zips. It then books a future visit the customer already named, unless that time was cancelled, and it marks a visit completed once the clock has passed its end.

The clock moves only forward. The advance request runs each enabled job once for every scheduled time the new clock crossed, then completes past visits. Those jobs write from templates. A job that already ran for that occurrence does not run again. Each shop change also writes an audit row, which the desk shows as Activity.

## End-to-End Workflow

```mermaid
sequenceDiagram
  actor Person
  participant Desk
  participant API
  participant Turn
  participant Model
  participant DB

  Person->>Desk: Open the page
  Desk->>API: GET /api/bootstrap
  API->>DB: Catch up stored requests and finish past visits
  API-->>Desk: Clock, schedule, threads, policies

  Person->>Desk: Send a message
  Desk->>API: POST /api/messages
  API->>Turn: Run one turn
  Turn->>DB: Save the message
  alt Owner sentence handled directly
    Turn->>DB: Open or close a day, add a zip, or save or remove a reminder
  else Model turn
    Turn->>Model: Instructions, shop context, and the audience tools
    loop While the model calls a tool
      Model-->>Turn: Tool call
      Turn->>DB: Booking, memory, quote, escalation, or an owner change
      Turn-->>Model: Tool result
    end
    Model-->>Turn: Reply
  end
  Turn->>DB: Save the reply
  API-->>Desk: Reply
  Desk->>API: GET /api/bootstrap
  Desk-->>Person: Updated thread and schedule

  Person->>Desk: Set the clock forward
  Desk->>API: POST /api/clock/advance
  API->>DB: Store the later time
  API->>DB: Run due jobs and complete visits whose end has passed
  API-->>Desk: New clock
  Desk->>API: GET /api/bootstrap
```

The clock never moves by itself and never moves backward. A booking still needs a time and a technician from the customer. A start before the clock is refused. A booking that mentions gas is refused.

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

## The Clock

It moves only when a person sets it: click the time, choose a later day and time, then Set clock.

## Try it

Open http://127.0.0.1:8000 on a fresh database. The clock is Thursday, November 12, 2026, 4:00 PM Pacific, and it only moves forward. The header says Open every day. James Okonkwo is selected, with 1842 N 92nd St and gate code 4481 already on his card. Schedule, newest first, shows Luis Alvarez on Friday at 9:00 AM with Brooke Hale, Elena Vasquez's completed faucet visit on November 4, then Sam Rivera's unscheduled $149 drain quote. Policies lists Monday–Sunday 8:00 AM–5:00 PM and zips 98103, 98115, 98125, 98133, and 98177. Automations lists the 7:00 AM rundown, the 5:00 PM day-before reminder, and the quote chase. Activity is empty.

## Automated tests

```sh
cd backend && .venv/bin/pytest
cd frontend && npm test
cd frontend && npm run e2e
```

None of them call a model API. Provider tests use saved payloads. The agent and the morning jobs use a scripted model.
