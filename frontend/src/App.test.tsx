import { render, screen, waitFor, within } from "@testing-library/react"
import userEvent from "@testing-library/user-event"
import { beforeEach, expect, test, vi } from "vitest"
import { App } from "./App"
import type { Shop } from "./types"

const shop: Shop = {
  business: {
    id: 1,
    name: "Northside Plumbing",
    owner_name: "Maria Chen",
    timezone: "America/Los_Angeles",
    phone: "206-555-0148",
  },
  clock: "2026-11-12T16:00:00-08:00",
  settings: { sunday_calls: true },
  shop_memories: [],
  technicians: [{ id: 2, name: "Brooke Hale", skills: ["drain", "fixture"] }],
  customers: [
    {
      id: 3,
      name: "James Okonkwo",
      phone: "206-555-0101",
      email: "james.okonkwo@example.com",
      address: "1842 N 92nd St, Seattle, WA 98115",
      memories: [{ id: 1, kind: "access", content: "Gate code 4481" }],
    },
    {
      id: 4,
      name: "Sam Rivera",
      phone: "206-555-0103",
      email: null,
      address: "1205 NE 115th St, Seattle, WA 98125",
      memories: [],
    },
  ],
  appointments: [
    {
      id: 8,
      customer_id: 4,
      technician_id: null,
      service: "drain",
      summary: "Kitchen sink draining slowly",
      status: "quoted",
      start: null,
      end: null,
      quote_amount: 149,
    },
  ],
  automations: [
    { id: 1, name: "Morning rundown", kind: "morning_rundown", enabled: true, schedule: { hour: 7, minute: 0 } },
  ],
  escalations: [
    {
      id: 7,
      customer_id: 3,
      reason: "Gas smell",
      detail: "Customer is still in the house.",
      status: "open",
      created_at: "2026-11-12T16:00:00-08:00",
      resolved_at: null,
    },
  ],
  audits: [
    {
      id: 1,
      actor: "system",
      action: "book_appointment",
      reasoning: "James asked to book the leak visit",
      created_at: "2026-11-12T16:00:00-08:00",
    },
  ],
  conversations: [
    {
      id: 1,
      customer_id: 3,
      kind: "customer",
      messages: [
        {
          id: 1,
          role: "customer",
          body: "Can you come look at a leak?",
          created_at: "2026-11-12T16:00:00-08:00",
        },
      ],
    },
    {
      id: 2,
      customer_id: null,
      kind: "owner",
      messages: [
        {
          id: 2,
          role: "owner",
          body: "We stopped doing Sunday calls",
          created_at: "2026-11-12T16:00:00-08:00",
        },
      ],
    },
    {
      id: 3,
      customer_id: 3,
      kind: "customer",
      messages: [
        {
          id: 4,
          role: "assistant",
          body: "You're booked Friday at 10:00 AM.",
          created_at: "2026-11-12T16:05:00-08:00",
        },
      ],
    },
  ],
}

beforeEach(() => {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      const method = init?.method ?? "GET"
      if (method === "GET" && url === "/api/bootstrap") return json(shop)
      if (method === "POST" && url === "/api/messages") return json({ reply: "Noted.", conversation_id: 1 })
      if (method === "POST" && url === "/api/customers") {
        return json({ id: 99, name: "Casey Brooks", phone: "206-555-0188", email: null, address: null, memories: [] })
      }
      if (method === "POST" && url === "/api/clock/advance") return json({ clock: "2026-11-13T07:00:00-08:00" })
      if (method === "POST" && url === "/api/escalations/7/resolve") {
        return json({ id: 7, status: "resolved" })
      }
      throw new Error(`unexpected ${method} ${url}`)
    }),
  )
})

test("the customer list and James's thread render from bootstrap", async () => {
  render(<App />)
  expect(await screen.findByRole("button", { name: "James Okonkwo" })).toHaveAttribute("aria-pressed", "true")
  expect(screen.getByRole("button", { name: "Sam Rivera" })).toBeInTheDocument()
  const customer = screen.getByRole("region", { name: "Customer" })
  expect(within(customer).getByText("Can you come look at a leak?")).toBeInTheDocument()
  expect(within(customer).getByText("You're booked Friday at 10:00 AM.")).toBeInTheDocument()
  expect(within(customer).getByText("access: Gate code 4481")).toBeInTheDocument()
  expect(screen.getByRole("time")).toHaveTextContent(/Thu, Nov 12, 4:00\s*PM/)
})

test("sending a message calls the customer audience", async () => {
  const user = userEvent.setup()
  render(<App />)
  const customer = await screen.findByRole("region", { name: "Customer" })
  await user.type(within(customer).getByLabelText("Message"), "The sink is leaking")
  await user.click(within(customer).getByRole("button", { name: "Send" }))
  await waitFor(() => {
    expect(posted("/api/messages")).toEqual([
      { audience: "customer", customer_id: 3, body: "The sink is leaking" },
    ])
  })
})

test("the new-caller form creates a customer", async () => {
  const user = userEvent.setup()
  render(<App />)
  await screen.findByRole("button", { name: "James Okonkwo" })
  await user.type(screen.getByLabelText("Name"), "Casey Brooks")
  await user.type(screen.getByLabelText("Phone"), "206-555-0188")
  await user.click(screen.getByRole("button", { name: "Add caller" }))
  await waitFor(() => {
    expect(posted("/api/customers")).toEqual([{ name: "Casey Brooks", phone: "206-555-0188" }])
  })
})

test("the owner composer calls the owner audience", async () => {
  const user = userEvent.setup()
  render(<App />)
  const owner = await screen.findByRole("region", { name: "Owner" })
  await user.type(within(owner).getByLabelText("Message"), "We stopped doing Sunday calls")
  await user.click(within(owner).getByRole("button", { name: "Send" }))
  await waitFor(() => {
    expect(posted("/api/messages")).toEqual([{ audience: "owner", body: "We stopped doing Sunday calls" }])
  })
})

test("clock controls advance to morning, four hours, and one day", async () => {
  const user = userEvent.setup()
  render(<App />)
  await screen.findByRole("button", { name: "Next morning" })
  await clickClock(user, "Next morning", [{ to: "next_morning" }])
  await clickClock(user, "Four hours", [{ to: "next_morning" }, { hours: 4 }])
  await clickClock(user, "One day", [{ to: "next_morning" }, { hours: 4 }, { to: "next_day" }])
})

test("an activity row shows the action and the reason", async () => {
  render(<App />)
  expect(
    await screen.findByText(
      (_, element) =>
        element?.tagName === "LI" && element.textContent === "book_appointment: James asked to book the leak visit",
    ),
  ).toBeInTheDocument()
})

test("a blank message is not sent", async () => {
  const user = userEvent.setup()
  render(<App />)
  const customer = await screen.findByRole("region", { name: "Customer" })
  await user.type(within(customer).getByLabelText("Message"), "   ")
  await user.click(within(customer).getByRole("button", { name: "Send" }))
  expect(posted("/api/messages")).toEqual([])
  expect(within(customer).getByLabelText("Message")).toHaveValue("   ")
})

test("a validation error from the server is shown", async () => {
  const user = userEvent.setup()
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      const method = init?.method ?? "GET"
      if (method === "GET" && url === "/api/bootstrap") return json(shop)
      if (method === "POST" && url === "/api/customers") {
        return new Response(JSON.stringify({ detail: [{ msg: "Field required" }] }), { status: 422 })
      }
      throw new Error(`unexpected ${method} ${url}`)
    }),
  )
  render(<App />)
  await screen.findByRole("button", { name: "James Okonkwo" })
  await user.type(screen.getByLabelText("Name"), "Casey Brooks")
  await user.type(screen.getByLabelText("Phone"), "206-555-0188")
  await user.click(screen.getByRole("button", { name: "Add caller" }))
  expect(await screen.findByRole("alert")).toHaveTextContent("Field required")
  expect(screen.getByLabelText("Name")).toHaveValue("Casey Brooks")
})

test("a second submit does not send the message twice", async () => {
  let release = () => {}
  const gate = new Promise<void>((resolve) => {
    release = resolve
  })
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input)
      const method = init?.method ?? "GET"
      if (method === "GET" && url === "/api/bootstrap") return json(shop)
      if (method === "POST" && url === "/api/messages") {
        await gate
        return json({ reply: "Noted.", conversation_id: 1 })
      }
      throw new Error(`unexpected ${method} ${url}`)
    }),
  )
  const { fireEvent } = await import("@testing-library/react")
  render(<App />)
  const customer = await screen.findByRole("region", { name: "Customer" })
  fireEvent.change(within(customer).getByLabelText("Message"), { target: { value: "The sink is leaking" } })
  const form = within(customer).getByLabelText("Message").closest("form")
  expect(form).not.toBeNull()
  fireEvent.submit(form!)
  fireEvent.submit(form!)
  release()
  await waitFor(() => expect(posted("/api/messages")).toHaveLength(1))
})

test("resolving an escalation calls the resolve endpoint", async () => {
  const user = userEvent.setup()
  render(<App />)
  await user.click(await screen.findByRole("button", { name: "Resolve" }))
  await waitFor(() => {
    expect(posted("/api/escalations/7/resolve")).toEqual([{}])
  })
})

async function clickClock(
  user: ReturnType<typeof userEvent.setup>,
  name: string,
  expected: ({ to: string } | { hours: number })[],
) {
  await user.click(screen.getByRole("button", { name }))
  await waitFor(() => {
    expect(posted("/api/clock/advance")).toEqual(expected)
    expect(screen.getByRole("button", { name: "Four hours" })).toBeEnabled()
  })
}

function posted(url: string): unknown[] {
  return vi
    .mocked(fetch)
    .mock.calls.filter(([input, init]) => String(input) === url && (init?.method ?? "GET") === "POST")
    .map(([, init]) => JSON.parse(String(init?.body)))
}

function json(body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  })
}
