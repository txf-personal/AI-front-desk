# Notes

## Assumptions

One shop, Northside Plumbing, in America/Los_Angeles. The clock starts Thursday, November 12, 2026, at 4:00 PM, and only moves when someone advances it. There is no login. The page is the demo.

The model decides and drafts. The shop owns state. A booking copies the address and the active access notes, checks hours, skills, and the service-area zip, and writes an audit row with the model's reason. The activity list shows that action, the reason, and the saved inputs. A visit or a quote outside the service area writes nothing. If the turn mentions gas, the shop refuses the booking. A quote with no book price writes nothing. Customer facts stay on that customer. Shop policies are visible to both the customer and the owner. A message row is the delivery. There is no SMS, email, or calendar provider.

Sunday calls start on. When they are on and Sunday hours are empty, Sunday uses Monday's 8:00–5:00 window.

## Cuts

No accounts, no real channels, no multi-shop screen, no vector search, no conversation summaries, no travel time, no payments, and no websockets. The only job the owner can create is the weekly quote report. Arbitrary schedules were cut. `business_id` is on the rows so a second shop has a place to go. This demo loads one.

## Another week

Put SMS and email behind the same message port, with the provider's id stored so a retry does not send twice. Add a folder of scripted customer conversations and score the real model against them. Teach scheduling about parts and drive time. Run due jobs on a worker instead of inside the clock request.

## Thousands of businesses

Keep every query and every job on one `business_id`. Cap the tokens in a turn. Give each outbound message an idempotency key. Leave memories in the prompt while they fit. Retrieve only after they do not.

## How AI tools were used

I use Cursor as a productivity booster, not a substitute for engineering judgment.

I start by thoroughly understanding the requirements, goals, and non-goals, then take ownership of the initial design and implementation plan. Once I have a clear direction, I provide Cursor with sufficient context to ensure it understands the requirements and architectural decisions.

I use AI to accelerate coding, generate test cases, debug issues, and explore alternative solutions. However, I always review and validate the generated code, taking full ownership of the architecture, code quality, correctness, and performance.

Ultimately, AI helps me move faster, but the engineering decisions and final deliverables remain my responsibility.

## How it was checked

`pytest` uses an in-memory database and never opens a network socket. `npm test` renders the desk with a mocked `fetch`. Playwright runs the click-through against the API and a scripted model: the booking, the Sunday policy, the gas handoff, the morning reminder, rundown, and chase, and the Friday report. The same paths were clicked in a browser. `./start.sh` is the laptop command. `docker compose up --build` is the container command.
