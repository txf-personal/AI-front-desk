# Notes

## Assumptions

One shop, Northside Plumbing, in America/Los_Angeles. There is no login. The page is the demo.

The clock starts Thursday, November 12, 2026, at 4:00 PM. It moves only when a person sets it: click the time, choose a later day and time, then Set clock. It never moves on its own, and it never moves backward.

A day can only be opened or closed as a whole day. An open day is 8:00 AM–5:00 PM. Opening or closing only part of a day is refused. Once the clock reaches a visit's end, that visit is completed.

## Cuts

No accounts, no real channels, no multi-shop screen, no vector search, no conversation summaries, no partial open day, no payments, and no websockets.

## Another week

Add login, so the owner and each customer are separate accounts instead of one open page. Load a second shop on the existing `business_id` rows. Put SMS, email, and a calendar behind the same message port, with the provider's id stored so a retry does not send twice. Take payments. Let the owner ask for jobs other than the weekly quote report, and let a day open or close for only part of the day. Add scripted customer conversations and score the real model against them. Teach scheduling about parts and drive time. Run due jobs on a worker instead of inside the clock request. Summarize long threads, and push the desk over a websocket instead of reloading after each send.

## Thousands of businesses

Keep every query and every job on one `business_id`. Cap the tokens in a turn. Give each outbound message an idempotency key. Leave memories in the prompt while they fit. Retrieve only after they do not.

## How AI tools were used

I use Cursor as a productivity booster, not a substitute for engineering judgment.

I start by thoroughly understanding the requirements, goals, and non-goals, then take ownership of the initial design and implementation plan. Once I have a clear direction, I provide Cursor with sufficient context to ensure it understands the requirements and architectural decisions.

I use AI to accelerate coding, generate test cases, debug issues, and explore alternative solutions. However, I always review and validate the generated code, taking full ownership of the architecture, code quality, correctness, and performance.

Ultimately, AI helps me move faster, but the engineering decisions and final deliverables remain my responsibility.

## How it was checked

None of the automated tests call a model API. `cd backend && .venv/bin/pytest` uses an in-memory database and never opens a socket. It covers booking, hours, the service area, a gas booking being refused, memories, the clock, the day-before reminder, the same-day reminder, the rundown, the chase, and the Friday quote report. Provider tests use saved payloads. `cd frontend && npm test` renders the desk with a mocked `fetch`. `cd frontend && npm run e2e` runs Playwright against the API and a scripted model: book James, turn Sunday calls off, hand gas to the owner, then move the clock through the reminder, rundown, chase, and Friday report.

Manual tests also ran those paths against the real model. Each request and response is printed in the terminal for logging and debugging.

The following "Try it yourself" is an FYI, on a fresh shop started with `./start.sh` or `docker compose up --build`. The clock starts Thursday, November 12, 2026, at 4:00 PM.

## Try it yourself

1. As James, ask "what a leak visit costs". The answer is $129. Ask to "remember that the side door is unlocked." It stays on James's card.
2. Still as James, ask to "book the leak for Friday at 11:30 AM with Andre Williams." He does not have to repeat his address or gate code. Activity shows "Booked a visit," the reason, and those inputs.
3. As Priya, ask to "book a drain appointment tomorrow morning." The slots start before noon.
4. As the owner, say "We stopped doing Sunday calls." The header says Closed Sunday. The Policies section shows that sentence.
5. The shop already has this automation: "Every day at 5:00 PM, customers hear about tomorrow's visit." As the owner, say "remove Every day at 5:00 PM, customers hear about tomorrow's visit." That automation is removed. Then say "Every day at 9:00 PM, customers hear about tomorrow's visit." The new automation is saved. Then adjust the time to 9:00 PM, you will see reminders are automatically sent out.
6. As James, say "I smell gas and I am still in the house." An open escalation names him. Resolve it. The Friday leak is still the only new booking. If the model tries to book on that turn, the shop rejects it.
7. Change the time and set Friday, November 13, 7:00 AM. Earlier times stay disabled. Sam gets one chase for the $149 quote. Maria's rundown names both visits.
8. Set Friday, November 13, 11:00 AM. Luis's visit is completed. James's visit stays scheduled until 11:30 AM. The newest activity row is on top.
9. Add a caller. A phone number already on file is rejected. The pencil opens the contact form, and saving the phone and address closes it.
10. Set that caller's address to `900 Bellevue Way, Bellevue, WA 98004` and ask for a drain visit. The reply says the address is outside the area, and no visit is added.
11. Change the address to `100 Yesler Way, Seattle, WA 98104`. As the owner, say "Add service area 98104." The Policies section shows one service-area line that includes 98104. As the caller, ask for a faucet price and do not pick a time. A quoted row appears with no time. Book that faucet for Saturday at 9:00 AM with Brooke Hale. The quoted row becomes the visit.
12. As the owner, say "Close tomorrow." All Saturday visits will be cancelled, and the callers will be told.
13. As the owner, ask to "open Saturday from 9:00 AM to 12:00 PM." The desk refuses. Say "Open Saturday." Saturday is 8:00 AM–5:00 PM again.
