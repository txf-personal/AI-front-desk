import { expect, test, type Locator, type Page } from "@playwright/test"

test.describe.configure({ mode: "serial" })

test.beforeEach(async ({ page }) => {
  await page.goto("/")
  await expect(page.getByRole("button", { name: "James Okonkwo" })).toHaveAttribute("aria-pressed", "true")
})

test("the seeded shop shows the clock, people, schedule, and empty threads", async ({ page }) => {
  const desk = customer(page)
  const maria = owner(page)

  await expect(page.getByRole("heading", { name: "Northside Plumbing" })).toBeVisible()
  await expect(page.getByText("Maria Chen · 206-555-0148")).toBeVisible()
  await expect(page.getByText("Open every day")).toBeVisible()
  await expect(page.locator("time")).toHaveText(/Thu, Nov 12, 2026, 4:00\s*PM/)
  await expect(desk.getByText("1842 N 92nd St, Seattle, WA 98115")).toBeVisible()
  await expect(desk.getByRole("form", { name: "Contact" })).toHaveCount(0)
  await expect(desk.getByRole("button", { name: "Edit contact" })).toHaveAttribute("aria-expanded", "false")
  await expect(desk.getByText("access: Gate code 4481")).toBeVisible()
  await expect(desk.getByText("No messages yet.")).toBeVisible()
  await expect(maria.getByText("No messages yet.")).toBeVisible()
  await expect(maria.getByText("No escalations.")).toBeVisible()
  await expect(maria.getByText("No activity yet.")).toBeVisible()
  await expect(maria).toContainText("Monday–Sunday 8:00 AM–5:00 PM.")
  await expect(maria).toContainText("Service area 98103, 98115, 98125, 98133, 98177")

  for (const name of ["Priya Shah", "Sam Rivera", "Luis Alvarez", "Elena Vasquez"]) {
    await expect(page.getByRole("button", { name })).toBeVisible()
  }

  await page.getByRole("button", { name: "Priya Shah" }).click()
  await expect(desk.getByText("preference: Prefers morning appointments")).toBeVisible()
  await expect(desk.getByText("fact: Dog in the yard")).toBeVisible()
  await expect(desk.getByText("access: Gate code 4481")).toHaveCount(0)

  await page.getByRole("button", { name: "Sam Rivera" }).click()
  await expect(desk.getByText("No messages yet.")).toBeVisible()
  await expect(desk.getByText("Gate code 4481")).toHaveCount(0)

  await page.getByRole("button", { name: "James Okonkwo" }).click()
  await expect(desk.getByText("access: Gate code 4481")).toBeVisible()

  await expect(maria).toContainText(/Luis Alvarez/)
  await expect(maria).toContainText(/Brooke Hale/)
  await expect(maria).toContainText(/Main bathroom drain/)
  await expect(maria).toContainText(/Fri, Nov 13, 2026, 9:00\s*AM/)
  await expect(maria).toContainText(/Sam Rivera/)
  await expect(maria).toContainText("$149")
  await expect(maria).toContainText("Kitchen sink draining slowly")
  await expect(maria).toContainText("Elena Vasquez")
  await expect(maria).toContainText("completed")
  await expect(maria).toContainText("Kitchen faucet cartridge")
  await expect(maria).toContainText("Morning rundown")
  await expect(maria).toContainText("Day-before reminder")
  await expect(maria).toContainText("Quiet quote chase")
  await expect(maria).toContainText("Every day at 5:00 PM, customers hear about tomorrow's visit.")
})

test("blank and half-filled forms do not change the shop", async ({ page }) => {
  const desk = customer(page)
  await desk.getByLabel("Message").fill("   ")
  await desk.getByRole("button", { name: "Send" }).click()
  await expect(desk.getByLabel("Message")).toHaveValue("   ")
  await expect(desk.getByText("No messages yet.")).toBeVisible()

  const form = page.getByRole("form", { name: "New caller" })
  await form.getByLabel("Name").fill("Casey Brooks")
  await form.getByRole("button", { name: "Add caller" }).click()
  await expect(page.getByRole("button", { name: "Casey Brooks" })).toHaveCount(0)
  await expect(form.getByLabel("Name")).toHaveValue("Casey Brooks")

  await form.getByLabel("Name").fill("   ")
  await form.getByLabel("Phone").fill("206-555-0199")
  await form.getByRole("button", { name: "Add caller" }).click()
  await expect(page.getByRole("button", { name: "Casey Brooks" })).toHaveCount(0)
  await expect(form.getByLabel("Phone")).toHaveValue("206-555-0199")
})

test("a new caller is trimmed and a duplicate phone stays on the form", async ({ page }) => {
  const maria = owner(page)
  const form = page.getByRole("form", { name: "New caller" })
  await form.getByLabel("Name").fill("  Casey Brooks  ")
  await form.getByLabel("Phone").fill(" 206-555-0188 ")
  await form.getByRole("button", { name: "Add caller" }).click()
  await expect(page.getByRole("button", { name: "Casey Brooks" })).toHaveAttribute("aria-pressed", "true")
  await expect(form.getByLabel("Name")).toHaveValue("")
  await expect(form.getByLabel("Phone")).toHaveValue("")
  await expect(maria).toContainText("Added a caller: New caller recorded")

  await form.getByLabel("Name").fill("Copy of James")
  await form.getByLabel("Phone").fill("206-555-0101")
  await form.getByRole("button", { name: "Add caller" }).click()
  await expect(page.getByRole("alert")).toHaveText("phone is already on file")
  await expect(page.getByRole("button", { name: "Copy of James" })).toHaveCount(0)
  await expect(form.getByLabel("Name")).toHaveValue("Copy of James")
  await expect(page.getByRole("button", { name: "Casey Brooks" })).toHaveCount(1)
})

test("booking James does not leak into Sam's thread", async ({ page }) => {
  const desk = customer(page)
  const maria = owner(page)
  await send(desk, "Please book the leak for Friday at 10 with Andre Williams.")
  await expect(desk).toContainText("You're booked Friday at 10:00 AM. I have 1842 N 92nd St and gate code 4481.")
  await expect(maria.locator("li", { hasText: "Booked a visit: James asked to book the leak visit" })).toHaveCount(1)
  await expect(scheduled(maria, "James Okonkwo", "leak")).toHaveCount(1)

  await page.getByRole("button", { name: "Sam Rivera" }).click()
  await expect(desk.getByText("You're booked Friday at 10:00 AM. I have 1842 N 92nd St and gate code 4481.")).toHaveCount(0)
  await expect(desk.getByText("No messages yet.")).toBeVisible()

  await page.getByRole("button", { name: "James Okonkwo" }).click()
  await expect(desk).toContainText("gate code 4481")
})

test("an owner policy stays off the customer thread", async ({ page }) => {
  const desk = customer(page)
  const maria = owner(page)
  await send(maria, "We stopped doing Sunday calls")
  await expect(maria).toContainText("Sunday calls are off. I'll remember that.")
  await expect(page.getByText("Closed Sunday")).toBeVisible()
  await expect(maria.locator(".bubble", { hasText: "We stopped doing Sunday calls" })).toHaveCount(1)
  await expect(policies(maria).filter({ hasText: "We stopped doing Sunday calls" })).toHaveCount(1)
  await expect(desk.getByText("We stopped doing Sunday calls")).toHaveCount(0)
  await expect(maria).toContainText("Changed a setting: Owner said Sunday calls are over")
  await expect(maria).toContainText("Saved a note: Owner said Sunday calls are over")
})

test("the owner can save the Friday quote report", async ({ page }) => {
  const maria = owner(page)
  await send(maria, "Every Friday, tell me who is still waiting on a quote.")
  await expect(maria).toContainText("I'll report open quotes every Friday at 7:30.")
  await expect(maria).toContainText("Quotes still waiting")
  await expect(maria).toContainText("Every Friday at 7:30 AM, list quotes that are still waiting.")
  await expect(maria).toContainText("Saved an automation: Owner asked for a weekly quote report")
  await expect(maria.locator(".bubble", { hasText: /quotes still waiting on Friday/ })).toHaveCount(0)
})

test("gas is handed to the owner and does not book another visit", async ({ page }) => {
  const desk = customer(page)
  const maria = owner(page)
  await send(desk, "I smell gas and I am still in the house.")
  await expect(desk).toContainText("I'm handing this to Maria. Please leave the house.")
  await expect(maria).toContainText("Gas smell")
  await expect(maria).toContainText("Customer smells gas and is still in the house.")
  await expect(maria).toContainText("Escalated: Gas smell")
  await expect(scheduled(maria, "James Okonkwo", "leak")).toHaveCount(1)

  await page.getByRole("button", { name: "Resolve" }).click()
  await expect(page.getByRole("button", { name: "Resolve" })).toHaveCount(0)
  await expect(maria).toContainText("resolved")
  await expect(maria).toContainText("Resolved an escalation: Owner resolved the escalation")
})

test("next morning sends the reminder, rundown, and chase, and not the weekly report", async ({ page }) => {
  const desk = customer(page)
  const maria = owner(page)
  await setClock(page, "Friday, November 13, 2026", "7", "00", "AM", /Fri, Nov 13, 2026, 7:00\s*AM/)

  await page.getByRole("button", { name: "Luis Alvarez" }).click()
  await expect(desk).toContainText(
    "Luis Alvarez, reminder from Northside Plumbing: Brooke Hale is scheduled Friday, November 13 at 9:00 AM for Main bathroom drain.",
  )

  await page.getByRole("button", { name: "James Okonkwo" }).click()
  await expect(desk).toContainText(
    "James Okonkwo, reminder from Northside Plumbing: Andre Williams is scheduled Friday, November 13 at 10:00 AM for Leak under the kitchen sink.",
  )

  await page.getByRole("button", { name: "Sam Rivera" }).click()
  await expect(desk.locator(".bubble", { hasText: /your Drain cleaning quote for \$149 is still waiting/ })).toHaveCount(1)
  await expect(desk).toContainText("Kitchen sink draining slowly")

  await expect(maria).toContainText("Maria Chen, morning rundown for Friday, November 13:")
  await expect(maria).toContainText("Luis Alvarez at 9:00 AM with Brooke Hale (Main bathroom drain)")
  await expect(maria).toContainText("James Okonkwo at 10:00 AM with Andre Williams (Leak under the kitchen sink)")
  await expect(maria.locator(".bubble", { hasText: /quotes still waiting on Friday/ })).toHaveCount(0)
  await expect(maria).toContainText("Morning rundown:")
  await expect(maria).toContainText("Appointment reminder:")
  await expect(maria).toContainText("Quote follow-up:")
})

test("four more hours delivers the Friday quote report without naming Luis", async ({ page }) => {
  const maria = owner(page)
  await setClock(page, "Friday, November 13, 2026", "11", "00", "AM", /Fri, Nov 13, 2026, 11:00\s*AM/)
  const report = maria.locator(".bubble", { hasText: /quotes still waiting on Friday, November 13/ })
  await expect(report).toHaveCount(1)
  await expect(report).toContainText("Sam Rivera")
  await expect(report).not.toContainText("Luis Alvarez")
  await expect(maria).toContainText("Quote report: Weekly report lists quotes that are still waiting.")
})

test("the next day does not repeat Friday's report or Sam's chase", async ({ page }) => {
  const desk = customer(page)
  const maria = owner(page)
  await setClock(page, "Saturday, November 14, 2026", "11", "00", "AM", /Sat, Nov 14, 2026, 11:00\s*AM/)
  await expect(maria.locator(".bubble", { hasText: /quotes still waiting on Friday/ })).toHaveCount(1)
  await expect(maria).toContainText("morning rundown for Saturday, November 14: no visits booked")

  await page.getByRole("button", { name: "Sam Rivera" }).click()
  await expect(desk.locator(".bubble", { hasText: /your Drain cleaning quote for \$149 is still waiting/ })).toHaveCount(1)
})

test("a later note does not book a second visit or add another policy", async ({ page }) => {
  const desk = customer(page)
  const maria = owner(page)
  await send(desk, "Just saying hello")
  await expect(desk).toContainText("I can take a booking, a quote, or a note for Maria.")
  const leak = maria.locator("tr", { hasText: "James Okonkwo" }).filter({ hasText: "leak" })
  await expect(leak).toHaveCount(1)
  await expect(leak).toContainText("completed")

  await send(maria, "Thanks")
  await expect(maria).toContainText("I can take a booking, a quote, or a note for Maria.")
  await expect(maria.locator(".bubble", { hasText: "We stopped doing Sunday calls" })).toHaveCount(1)
  await expect(policies(maria).filter({ hasText: "We stopped doing Sunday calls" })).toHaveCount(1)
})

function customer(page: Page): Locator {
  return page.getByRole("region", { name: "Customer" })
}

function owner(page: Page): Locator {
  return page.getByRole("region", { name: "Owner" })
}

function policies(region: Locator): Locator {
  return region.locator("section.card", { hasText: "Policies" }).locator("li")
}

function scheduled(region: Locator, name: string, service: string): Locator {
  return region.locator("tr", { hasText: name }).filter({ hasText: service }).filter({ hasText: "scheduled" })
}

async function send(region: Locator, body: string) {
  await region.getByLabel("Message").fill(body)
  await region.getByRole("button", { name: "Send" }).click()
  await expect(region.getByLabel("Message")).toHaveValue("")
}

async function setClock(page: Page, day: string, hour: string, minute: string, suffix: "AM" | "PM", clock: RegExp) {
  const menu = page.getByRole("dialog", { name: "Move the shop clock forward" })
  if (!(await menu.isVisible())) await page.getByRole("button", { name: /Shop clock/ }).click()
  await menu.getByRole("button", { name: day, exact: true }).click()
  await menu.getByRole("button", { name: `${hour} o'clock`, exact: true }).click()
  await menu.getByLabel("Minute").selectOption(minute)
  await menu.getByRole("button", { name: suffix, exact: true }).click()
  await menu.getByRole("button", { name: "Set clock" }).click()
  await expect(page.locator("time")).toHaveText(clock)
}
