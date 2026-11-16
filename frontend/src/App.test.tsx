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
  settings: {
    sunday_calls: true,
    hours: {
      monday: ["08:00", "17:00"],
      tuesday: ["08:00", "17:00"],
      wednesday: ["08:00", "17:00"],
      thursday: ["08:00", "17:00"],
      friday: ["08:00", "17:00"],
      saturday: null,
      sunday: null,
    },
    service_area: ["98103", "98115"],
  },
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
    {
      id: 1,
      name: "Morning rundown",
      kind: "morning_rundown",
      enabled: true,
      schedule: { hour: 7, minute: 0 },
      description: "Each morning at 7:00, tell the owner who is booked that day.",
    },
    {
      id: 2,
      name: "Day-before reminder",
      kind: "appointment_reminder",
      enabled: true,
      schedule: { hour: 17, minute: 0 },
    },
    {
      id: 3,
      name: "Quiet quote chase",
      kind: "quote_followup",
      enabled: true,
      schedule: { hour: 7, minute: 0, quiet_days: 2, max_sends: 2 },
    },
    {
      id: 4,
      name: "Quotes still waiting",
      kind: "quote_report",
      enabled: true,
      schedule: { weekday: "friday", hour: 7, minute: 30 },
    },
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
      payload: {
        customer_id: 3,
        service: "leak",
        technician_id: 2,
        start: "2026-11-13T10:00:00-08:00",
        price: 129,
      },
      created_at: "2026-11-12T16:00:00-08:00",
    },
    {
      id: 2,
      actor: "system",
      action: "appointment_reminder",
      reasoning: "Day-before reminder lists visits scheduled on 2026-11-16.",
      payload: {
        automation_id: 2,
        dedupe_key: "reminder:2026-11-15",
        date: "2026-11-16",
        visits: 2,
      },
      created_at: "2026-11-15T17:00:00-08:00",
    },
    {
      id: 3,
      actor: "owner",
      action: "set_setting",
      reasoning: "Saturday is closed",
      payload: {
        key: "hours",
        value: { monday: ["08:00", "17:00"], saturday: null },
      },
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
        {
          id: 5,
          role: "assistant",
          body: "Sunday openings:\n\n1. **8:00 - 9:30**\n\n2. **8:30 - 10:00**\n\nThe price is **$149**.",
          created_at: "2026-11-12T16:10:00-08:00",
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
  localStorage.removeItem("northside-split")
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
      if (method === "PATCH" && url === "/api/customers/3") {
        return json({ id: 3, name: "James Okonkwo", phone: "206-555-0199", email: null, address: "9 Pike St", memories: [] })
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
  expect(screen.getByRole("time")).toHaveTextContent(/Thu, Nov 12, 2026, 4:00\s*PM/)
})

test("the column seam moves the desk", async () => {
  const user = userEvent.setup()
  render(<App />)
  const seam = await screen.findByRole("separator", { name: "Resize the columns" })
  expect(seam).toHaveAttribute("aria-valuenow", "40")
  seam.focus()
  await user.keyboard("{ArrowRight}")
  expect(Number(seam.getAttribute("aria-valuenow"))).toBeGreaterThan(40)
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
  const caller = screen.getByRole("form", { name: "New caller" })
  await user.type(within(caller).getByLabelText("Name"), "Casey Brooks")
  await user.type(within(caller).getByLabelText("Phone"), "206-555-0188")
  await user.click(within(caller).getByRole("button", { name: "Add caller" }))
  await waitFor(() => {
    expect(posted("/api/customers")).toEqual([{ name: "Casey Brooks", phone: "206-555-0188" }])
  })
})

test("a phone without dashes is saved with dashes", async () => {
  const user = userEvent.setup()
  render(<App />)
  await user.click(await screen.findByRole("button", { name: "Edit contact" }))
  const contact = screen.getByRole("form", { name: "Contact" })
  const phone = within(contact).getByLabelText("Phone")
  await user.clear(phone)
  await user.type(phone, "2065550199")
  await user.click(within(contact).getByRole("button", { name: "Save" }))
  await waitFor(() => {
    expect(sent("PATCH", "/api/customers/3")).toEqual([
      { phone: "206-555-0199", address: "1842 N 92nd St, Seattle, WA 98115" },
    ])
  })
})

test("the contact form saves a trimmed phone and address", async () => {
  const user = userEvent.setup()
  render(<App />)
  expect(await screen.findByRole("button", { name: "Edit contact" })).toHaveAttribute("aria-expanded", "false")
  expect(screen.getByText("1842 N 92nd St, Seattle, WA 98115")).toBeInTheDocument()
  expect(screen.queryByRole("form", { name: "Contact" })).not.toBeInTheDocument()
  await user.click(screen.getByRole("button", { name: "Edit contact" }))
  const contact = screen.getByRole("form", { name: "Contact" })
  const phone = within(contact).getByLabelText("Phone")
  const address = within(contact).getByLabelText("Address")
  expect(phone).toHaveValue("206-555-0101")
  expect(address).toHaveValue("1842 N 92nd St, Seattle, WA 98115")
  expect(within(contact).getByRole("button", { name: "Save" })).toBeDisabled()
  await user.clear(phone)
  await user.type(phone, " 206-555-0199 ")
  await user.clear(address)
  await user.type(address, "  9 Pike St, Seattle, WA 98115  ")
  await user.click(within(contact).getByRole("button", { name: "Save" }))
  await waitFor(() => {
    expect(sent("PATCH", "/api/customers/3")).toEqual([
      { phone: "206-555-0199", address: "9 Pike St, Seattle, WA 98115" },
    ])
  })
})

test("the new-caller form sends a trimmed address", async () => {
  const user = userEvent.setup()
  render(<App />)
  await screen.findByRole("button", { name: "James Okonkwo" })
  const caller = screen.getByRole("form", { name: "New caller" })
  await user.type(within(caller).getByLabelText("Name"), "Casey Brooks")
  await user.type(within(caller).getByLabelText("Phone"), "206-555-0188")
  await user.type(within(caller).getByLabelText("Address"), "  9 Pike St, Seattle, WA 98115  ")
  await user.click(within(caller).getByRole("button", { name: "Add caller" }))
  await waitFor(() => {
    expect(posted("/api/customers")).toEqual([
      { name: "Casey Brooks", phone: "206-555-0188", address: "9 Pike St, Seattle, WA 98115" },
    ])
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

test("shop clock opens a calendar and clock that only move forward", async () => {
  const user = userEvent.setup()
  render(<App />)
  const trigger = await screen.findByRole("button", { name: /Shop clock/ })
  expect(screen.queryByRole("dialog", { name: "Move the shop clock forward" })).not.toBeInTheDocument()

  await user.click(trigger)
  const menu = screen.getByRole("dialog", { name: "Move the shop clock forward" })
  expect(menu).toHaveTextContent("The clock only moves forward.")
  expect(within(menu).getByRole("grid", { name: "Calendar" })).toBeInTheDocument()
  expect(within(menu).getByRole("button", { name: "Previous month" })).toBeDisabled()
  expect(within(menu).getByRole("button", { name: "Wednesday, November 11, 2026" })).toBeDisabled()
  expect(within(menu).getByRole("button", { name: "Set clock" })).toBeDisabled()

  await user.click(within(menu).getByRole("button", { name: "3 o'clock" }))
  expect(within(menu).getByRole("button", { name: "Set clock" })).toBeDisabled()

  await user.click(within(menu).getByRole("button", { name: "Friday, November 13, 2026" }))
  await user.click(within(menu).getByRole("button", { name: "7 o'clock" }))
  await user.click(within(menu).getByRole("button", { name: "AM" }))
  await user.selectOptions(within(menu).getByLabelText("Minute"), "0")
  await user.click(within(menu).getByRole("button", { name: "Set clock" }))
  await waitFor(() => {
    expect(posted("/api/clock/advance")).toEqual([{ until: "2026-11-13T07:00" }])
  })
  expect(screen.queryByRole("dialog", { name: "Move the shop clock forward" })).not.toBeInTheDocument()

  await user.click(screen.getByRole("button", { name: /Shop clock/ }))
  await user.keyboard("{Escape}")
  expect(screen.queryByRole("grid", { name: "Calendar" })).not.toBeInTheDocument()
})

test("a reply with numbered lines renders an ordered list", async () => {
  render(<App />)
  const customer = await screen.findByRole("region", { name: "Customer" })
  const bubble = within(customer).getByText("Sunday openings:").closest(".bubble")
  expect(bubble).not.toBeNull()
  const box = bubble as HTMLElement
  const lists = within(box).getAllByRole("list")
  expect(lists).toHaveLength(1)
  expect(lists[0].tagName).toBe("OL")
  expect(lists[0]).toHaveAttribute("start", "1")
  expect(within(box).getAllByRole("listitem").map((item) => item.textContent)).toEqual([
    "8:00 - 9:30",
    "8:30 - 10:00",
  ])
  expect(within(box).getAllByRole("strong").map((item) => item.textContent)).toEqual([
    "8:00 - 9:30",
    "8:30 - 10:00",
    "$149",
  ])
  expect(box).not.toHaveTextContent("**")
})

test("an activity row shows the action, the reason, and the inputs", async () => {
  render(<App />)
  const booked = await screen.findByText(
    (_, element) => element?.tagName === "LI" && (element.textContent?.includes("Booked a visit:") ?? false),
  )
  expect(booked).toHaveTextContent("Booked a visit: James asked to book the leak visit")
  expect(booked).toHaveTextContent("James Okonkwo")
  expect(booked).toHaveTextContent("Leak")
  expect(booked).toHaveTextContent("Brooke Hale")
  expect(booked).toHaveTextContent("$129")
  expect(booked).not.toHaveTextContent("technician_id")

  const reminder = screen.getByText(
    (_, element) => element?.tagName === "LI" && (element.textContent?.includes("Appointment reminder:") ?? false),
  )
  expect(reminder).toHaveTextContent("Mon, Nov 16, 2026 · 2 visits")
  expect(reminder).not.toHaveTextContent("dedupe_key")
  expect(reminder).not.toHaveTextContent("automation_id")

  const hours = screen.getByText(
    (_, element) => element?.tagName === "LI" && (element.textContent?.includes("Changed a setting:") ?? false),
  )
  expect(hours).toHaveTextContent("Monday 8:00–17:00, Saturday closed")
})

test("automations and policies are readable", async () => {
  render(<App />)
  expect(await screen.findByText("Every morning at 7:00 AM, the owner hears who is booked.")).toBeInTheDocument()
  expect(screen.getByText("Every day at 5:00 PM, customers hear about tomorrow's visit.")).toBeInTheDocument()
  expect(screen.getByText("Every day at 7:00 AM, after 2 days with no reply, at most 2 follow-ups.")).toBeInTheDocument()
  expect(screen.getByText("Every Friday at 7:30 AM, list quotes that are still waiting.")).toBeInTheDocument()
  expect(screen.queryByText(/morning_rundown/)).not.toBeInTheDocument()
  expect(screen.getByText("Closed Saturday")).toBeInTheDocument()
  expect(screen.queryByText("Sunday calls on")).not.toBeInTheDocument()
  expect(screen.queryByText("Sunday calls: off")).not.toBeInTheDocument()
  expect(screen.getByText("Monday–Friday 8:00 AM–5:00 PM. Saturday closed. Sunday 8:00 AM–5:00 PM.")).toBeInTheDocument()
  expect(screen.getByText("Service area 98103, 98115")).toBeInTheDocument()
  expect(screen.queryByText("No shop policies yet.")).not.toBeInTheDocument()
})

test("a service-area policy joins the service area line", async () => {
  const memories = shop.shop_memories
  shop.shop_memories = [
    { id: 9, kind: "policy", content: "Add service area 98104 is now allowed." },
    { id: 10, kind: "policy", content: "We stopped doing Sunday calls" },
  ]
  try {
    render(<App />)
    expect(await screen.findByText("Service area 98103, 98115, 98104")).toBeInTheDocument()
    expect(screen.queryByText("Add service area 98104 is now allowed.")).not.toBeInTheDocument()
    const policies = screen.getByRole("heading", { name: "Policies" }).closest("section")
    expect(policies).not.toBeNull()
    expect(within(policies!).getByText("We stopped doing Sunday calls")).toBeInTheDocument()
  } finally {
    shop.shop_memories = memories
  }
})

test("a shorter stored day is shown as 8:00 AM to 5:00 PM", async () => {
  const hours = shop.settings.hours
  shop.settings.hours = {
    ...(hours as Record<string, unknown>),
    monday: ["08:00", "09:00"],
  }
  try {
    render(<App />)
    expect(await screen.findByText("Monday–Friday 8:00 AM–5:00 PM. Saturday closed. Sunday 8:00 AM–5:00 PM.")).toBeInTheDocument()
    expect(screen.queryByText(/9:00 AM/)).not.toBeInTheDocument()
  } finally {
    shop.settings.hours = hours
  }
})

test("the header names whichever weekdays are closed", async () => {
  const hours = shop.settings.hours
  shop.settings.hours = {
    monday: null,
    tuesday: null,
    wednesday: ["08:00", "17:00"],
    thursday: ["08:00", "17:00"],
    friday: ["08:00", "17:00"],
    saturday: ["08:00", "17:00"],
    sunday: ["08:00", "17:00"],
  }
  try {
    render(<App />)
    expect(await screen.findByText("Closed Monday and Tuesday")).toBeInTheDocument()
    expect(screen.getByText("Monday–Tuesday closed. Wednesday–Sunday 8:00 AM–5:00 PM.")).toBeInTheDocument()
    expect(screen.queryByText("Sunday calls: off")).not.toBeInTheDocument()
  } finally {
    shop.settings.hours = hours
  }
})

test("activity and the schedule list the newest first", async () => {
  const appointments = shop.appointments
  const audits = shop.audits
  shop.appointments = [
    {
      id: 1,
      customer_id: 3,
      technician_id: 2,
      service: "faucet",
      summary: "Older visit",
      status: "completed",
      start: "2026-11-04T10:00:00-08:00",
      end: "2026-11-04T11:00:00-08:00",
      quote_amount: null,
    },
    {
      id: 2,
      customer_id: 3,
      technician_id: 2,
      service: "drain",
      summary: "Newer visit",
      status: "completed",
      start: "2026-11-14T09:30:00-08:00",
      end: "2026-11-14T10:30:00-08:00",
      quote_amount: 149,
    },
    {
      id: 3,
      customer_id: 4,
      technician_id: null,
      service: "drain",
      summary: "Kitchen sink draining slowly",
      status: "quoted",
      start: null,
      end: null,
      quote_amount: 149,
    },
  ]
  shop.audits = [
    {
      id: 1,
      actor: "system",
      action: "book_appointment",
      reasoning: "older activity",
      payload: null,
      created_at: "2026-11-12T16:00:00-08:00",
    },
    {
      id: 2,
      actor: "system",
      action: "complete_appointment",
      reasoning: "newer activity",
      payload: null,
      created_at: "2026-11-17T10:00:00-08:00",
    },
  ]
  try {
    render(<App />)
    const table = await screen.findByRole("table")
    const rows = within(table).getAllByRole("row")
    expect(rows[1]).toHaveTextContent("Nov 14")
    expect(rows[2]).toHaveTextContent("Nov 4")
    expect(rows[3]).toHaveTextContent("Unscheduled")
    const activity = screen.getByRole("heading", { name: "Activity" }).closest("section")
    expect(activity).not.toBeNull()
    const items = within(activity!).getAllByRole("listitem")
    expect(items[0]).toHaveTextContent("newer activity")
    expect(items[1]).toHaveTextContent("older activity")
  } finally {
    shop.appointments = appointments
    shop.audits = audits
  }
})

test("the schedule is a table", async () => {
  render(<App />)
  const table = await screen.findByRole("table")
  for (const name of ["When", "Customer", "Service", "Status", "Technician", "Price", "Summary"]) {
    expect(within(table).getByRole("columnheader", { name })).toBeInTheDocument()
  }
  const row = within(table).getByRole("row", { name: /Sam Rivera/ })
  expect(row).toHaveTextContent("Unscheduled")
  expect(row).toHaveTextContent("Drain")
  expect(row).toHaveTextContent("quoted")
  expect(row).toHaveTextContent("$149")
  expect(row).toHaveTextContent("Kitchen sink draining slowly")
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
  const caller = screen.getByRole("form", { name: "New caller" })
  await user.type(within(caller).getByLabelText("Name"), "Casey Brooks")
  await user.type(within(caller).getByLabelText("Phone"), "206-555-0188")
  await user.click(within(caller).getByRole("button", { name: "Add caller" }))
  expect(await screen.findByRole("alert")).toHaveTextContent("Field required")
  expect(within(caller).getByLabelText("Name")).toHaveValue("Casey Brooks")
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

function posted(url: string): unknown[] {
  return sent("POST", url)
}

function sent(method: string, url: string): unknown[] {
  return vi
    .mocked(fetch)
    .mock.calls.filter(([input, init]) => String(input) === url && (init?.method ?? "GET") === method)
    .map(([, init]) => JSON.parse(String(init?.body)))
}

function json(body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  })
}
