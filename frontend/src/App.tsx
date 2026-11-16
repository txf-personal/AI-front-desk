import { Children, useEffect, useRef, useState, type CSSProperties, type FormEvent, type KeyboardEvent as ReactKeyboardEvent, type PointerEvent as ReactPointerEvent, type ReactNode } from "react"
import { advanceClock, createCustomer, getBootstrap, resolveEscalation, sendMessage, updateCustomer } from "./api"
import type { Appointment, Audit, ChatMessage, Shop } from "./types"

export function App() {
  const [shop, setShop] = useState<Shop | null>(null)
  const [selectedId, setSelectedId] = useState<number | null>(null)
  const [error, setError] = useState("")
  const [busy, setBusy] = useState(false)
  const pending = useRef(false)

  function show(data: Shop, selectId?: number) {
    setShop(data)
    setSelectedId((current) => {
      const preferred = selectId ?? current
      if (preferred != null && data.customers.some((customer) => customer.id === preferred)) return preferred
      return data.customers[0]?.id ?? null
    })
  }

  useEffect(() => {
    let gone = false
    getBootstrap()
      .then((data) => {
        if (!gone) show(data)
      })
      .catch((err: unknown) => {
        if (!gone) setError(messageOf(err))
      })
    return () => {
      gone = true
    }
  }, [])

  async function run(action: () => Promise<unknown>): Promise<boolean> {
    if (pending.current) return false
    pending.current = true
    setBusy(true)
    setError("")
    try {
      const selected = await action()
      show(await getBootstrap(), typeof selected === "number" ? selected : undefined)
      return true
    } catch (err: unknown) {
      setError(messageOf(err))
      return false
    } finally {
      pending.current = false
      setBusy(false)
    }
  }

  if (!shop) {
    return (
      <main>
        <h1>Northside Plumbing</h1>
        {error ? <p role="alert">{error}</p> : <p>Loading the shop.</p>}
      </main>
    )
  }

  const selected = shop.customers.find((customer) => customer.id === selectedId) ?? null
  const customerMessages = messagesFor(shop, "customer", selected?.id ?? null)
  const ownerMessages = messagesFor(shop, "owner", null)

  const status = shopStatus(shop.settings)

  return (
    <main>
      <header>
        <div>
          <p className="eyebrow">Front desk</p>
          <h1>{shop.business.name}</h1>
          <p>
            {shop.business.owner_name} · {shop.business.phone}
          </p>
        </div>
        <div className="clock-readout">
          <ShopClock
            clock={shop.clock}
            busy={busy}
            onAdvance={(body) => run(() => advanceClock(body))}
          />
          <p className={status.open ? "pill on" : "pill off"}>{status.label}</p>
        </div>
      </header>
      {error ? <p className="alert" role="alert">{error}</p> : null}
      <DeskSplit>
        <section className="pane" aria-label="Customer">
          {selected ? (
            <>
              <ContactForm
                key={selected.id}
                customer={selected}
                busy={busy}
                onSave={(phone, address) => run(() => updateCustomer(selected.id, { phone, address }))}
              />
              {selected.memories.length > 0 ? (
                <ul className="tags">
                  {selected.memories.map((memory) => (
                    <li key={memory.id}>
                      {memory.kind}: {memory.content}
                    </li>
                  ))}
                </ul>
              ) : null}
              <Thread messages={customerMessages} />
              <Composer
                label="Message"
                busy={busy}
                onSend={(body) =>
                  run(() => sendMessage({ audience: "customer", customer_id: selected.id, body }))
                }
              />
            </>
          ) : null}
          <div className="roster">
            <h2>Customers</h2>
            <ul className="people">
              {shop.customers.map((customer) => (
                <li key={customer.id}>
                  <button
                    type="button"
                    className="person"
                    aria-pressed={customer.id === selectedId}
                    onClick={() => setSelectedId(customer.id)}
                  >
                    <span className="mark" aria-hidden="true">{initials(customer.name)}</span>
                    <span className="person-copy">
                      <span className="person-name">{customer.name}</span>
                      <span className="phone" aria-hidden="true">{customer.phone}</span>
                    </span>
                  </button>
                </li>
              ))}
            </ul>
            <CallerForm
              busy={busy}
              onCreate={(name, phone, address) =>
                run(async () => (await createCustomer({ name, phone, ...(address ? { address } : {}) })).id)
              }
            />
          </div>
        </section>
        <section className="pane owner-pane" aria-label="Owner">
          <h2>Owner desk</h2>
          <Thread messages={ownerMessages} />
          <Composer label="Message" busy={busy} onSend={(body) => run(() => sendMessage({ audience: "owner", body }))} />
          <div className="cards">
            <section className="card">
              <h3>Escalations</h3>
              {shop.escalations.length === 0 ? <p className="empty">No escalations.</p> : (
                <ul className="stack">
                  {shop.escalations.map((item) => (
                    <li key={item.id}>
                      <strong>{item.reason}</strong>
                      <span className="meta">
                        {item.customer_id != null ? ` · ${customerName(shop, item.customer_id)}` : ""}
                      </span>
                      <span className="status" data-status={item.status}>{item.status}</span>
                      <p>{item.detail}</p>
                      {item.status === "open" ? (
                        <button type="button" disabled={busy} onClick={() => void run(() => resolveEscalation(item.id))}>
                          Resolve
                        </button>
                      ) : null}
                    </li>
                  ))}
                </ul>
              )}
            </section>
            <section className="card">
              <h3>Activity</h3>
              {shop.audits.length === 0 ? <p className="empty">No activity yet.</p> : (
                <ScrollBox className="log" pin="start" watch={shop.audits.map((audit) => audit.id).join()}>
                  <ul className="stack">
                    {newestFirst(shop.audits).map((audit) => {
                      const detail = activityDetail(shop, audit.payload)
                      return (
                        <li key={audit.id}>
                          <span className="action">{actionLabel(audit.action)}</span>
                          {": "}
                          {audit.reasoning}
                          {detail ? <span className="inputs">{detail}</span> : null}
                        </li>
                      )
                    })}
                  </ul>
                </ScrollBox>
              )}
            </section>
            <section className="card">
              <h3>Schedule</h3>
              <div className="schedule-wrap">
                <table className="schedule">
                  <thead>
                    <tr>
                      <th scope="col">When</th>
                      <th scope="col">Customer</th>
                      <th scope="col">Service</th>
                      <th scope="col">Status</th>
                      <th scope="col">Technician</th>
                      <th scope="col">Price</th>
                      <th scope="col">Summary</th>
                    </tr>
                  </thead>
                  <tbody>
                    {ordered(shop.appointments).map((job) => (
                      <tr key={job.id}>
                        <td>{formatWhen(job.start)}</td>
                        <td>{customerName(shop, job.customer_id)}</td>
                        <td>{serviceLabel(job.service)}</td>
                        <td><span className="status" data-status={job.status}>{job.status}</span></td>
                        <td>{technicianName(shop, job.technician_id)}</td>
                        <td>{job.quote_amount != null ? `$${job.quote_amount}` : ""}</td>
                        <td>{job.summary}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </section>
            <section className="card">
              <h3>Automations</h3>
              <ul className="stack">
                {shop.automations.map((job) => (
                  <li key={job.id}>
                    <span className="who">{job.name}</span>
                    {" "}
                    <span className="inputs">{automationSentence(job)}</span>
                  </li>
                ))}
              </ul>
            </section>
            <section className="card">
              <h3>Policies</h3>
              {policyLines(shop).length === 0 ? <p className="empty">No shop policies yet.</p> : (
                <ul className="stack">
                  {policyLines(shop).map((line) => (
                    <li key={line}>{line}</li>
                  ))}
                </ul>
              )}
            </section>
          </div>
        </section>
      </DeskSplit>
    </main>
  )
}

const SPLIT_KEY = "northside-split"
const MIN_PANE = 280

function readSplit(): number {
  try {
    const raw = localStorage.getItem(SPLIT_KEY)
    const value = raw == null ? 0.4 : Number(raw)
    if (!Number.isFinite(value)) return 0.4
    return Math.min(0.78, Math.max(0.22, value))
  } catch {
    return 0.4
  }
}

function clampSplit(value: number, width: number): number {
  const floor = width > 0 ? MIN_PANE / width : 0.22
  const min = Math.min(0.4, Math.max(0.22, floor))
  return Math.min(1 - min, Math.max(min, value))
}

function DeskSplit({ children }: { children: ReactNode }) {
  const desk = useRef<HTMLDivElement>(null)
  const [ratio, setRatio] = useState(readSplit)
  const drag = useRef<{ x: number; ratio: number } | null>(null)
  const [left, right] = Children.toArray(children)

  useEffect(() => {
    try {
      localStorage.setItem(SPLIT_KEY, String(ratio))
    } catch {
      /* the desk still resizes for this visit */
    }
  }, [ratio])

  function moveTo(clientX: number, origin: { x: number; ratio: number }) {
    const width = desk.current?.getBoundingClientRect().width ?? 0
    if (width <= 0) return
    setRatio(clampSplit(origin.ratio + (clientX - origin.x) / width, width))
  }

  function onPointerDown(event: ReactPointerEvent<HTMLDivElement>) {
    const origin = { x: event.clientX, ratio }
    const seam = event.currentTarget
    drag.current = origin
    seam.dataset.dragging = "true"
    if (desk.current) desk.current.dataset.dragging = "true"
    function onMove(ev: PointerEvent) {
      moveTo(ev.clientX, origin)
    }
    function onUp() {
      drag.current = null
      delete seam.dataset.dragging
      if (desk.current) delete desk.current.dataset.dragging
      window.removeEventListener("pointermove", onMove)
      window.removeEventListener("pointerup", onUp)
    }
    window.addEventListener("pointermove", onMove)
    window.addEventListener("pointerup", onUp)
    try {
      seam.setPointerCapture(event.pointerId)
    } catch {
      /* the window listener still follows the pointer */
    }
  }

  function onKeyDown(event: ReactKeyboardEvent<HTMLDivElement>) {
    if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return
    event.preventDefault()
    const width = desk.current?.getBoundingClientRect().width ?? 0
    const step = width > 0 ? 24 / width : 0.02
    const delta = event.key === "ArrowRight" ? step : -step
    setRatio((current) => clampSplit(current + delta, width))
  }

  const percent = Math.round(ratio * 1000) / 10

  return (
    <div className="desk" ref={desk} style={{ "--split": `${percent}%` } as CSSProperties}>
      {left}
      <div
        className="split"
        role="separator"
        aria-orientation="vertical"
        aria-label="Resize the columns"
        aria-valuemin={22}
        aria-valuemax={78}
        aria-valuenow={Math.round(percent)}
        tabIndex={0}
        onPointerDown={onPointerDown}
        onKeyDown={onKeyDown}
      />
      {right}
    </div>
  )
}

function ContactForm({
  customer,
  busy,
  onSave,
}: {
  customer: { name: string; phone: string; address: string | null }
  busy: boolean
  onSave: (phone: string, address: string | null) => Promise<boolean>
}) {
  const [open, setOpen] = useState(false)
  const [phone, setPhone] = useState(customer.phone)
  const [address, setAddress] = useState(customer.address ?? "")
  useEffect(() => {
    setPhone(customer.phone)
    setAddress(customer.address ?? "")
  }, [customer.phone, customer.address])
  const nextPhone = normalizePhone(phone)
  const nextAddress = address.trim()
  const unchanged = nextPhone === customer.phone && nextAddress === (customer.address ?? "")

  function close() {
    setPhone(customer.phone)
    setAddress(customer.address ?? "")
    setOpen(false)
  }

  async function submit(event: FormEvent) {
    event.preventDefault()
    if (!nextPhone || unchanged) return
    if (await onSave(nextPhone, nextAddress || null)) setOpen(false)
  }

  return (
    <div className="identity">
      <div className="who-line">
        <h2>{customer.name}</h2>
        <button
          type="button"
          className="pencil"
          aria-label="Edit contact"
          aria-expanded={open}
          disabled={busy}
          onClick={() => (open ? close() : setOpen(true))}
        >
          <PencilIcon />
        </button>
      </div>
      {open ? (
        <form className="contact" aria-label="Contact" onSubmit={submit}>
          <label>
            Phone
            <input
              value={phone}
              onChange={(event) => setPhone(event.target.value)}
              onBlur={() => setPhone(normalizePhone(phone))}
            />
          </label>
          <label>
            Address
            <input value={address} onChange={(event) => setAddress(event.target.value)} />
          </label>
          <button type="submit" disabled={busy || !nextPhone || unchanged}>Save</button>
        </form>
      ) : (
        <div className="contact-read">
          <p className="meta">{customer.phone}</p>
          {customer.address ? <p className="meta">{customer.address}</p> : null}
        </div>
      )}
    </div>
  )
}

function normalizePhone(value: string): string {
  const digits = value.replace(/\D/g, "")
  const local = digits.length === 11 && digits.startsWith("1") ? digits.slice(1) : digits
  if (local.length !== 10) return value.trim()
  return `${local.slice(0, 3)}-${local.slice(3, 6)}-${local.slice(6)}`
}

function PencilIcon() {
  return (
    <svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true">
      <path
        fill="currentColor"
        d="M11.3 1.7a1.4 1.4 0 0 1 2 2L5.8 11.2 3.2 12l.8-2.6 7.3-7.7Z"
      />
      <path fill="currentColor" d="M2.2 13.4h11.6V14.6H2.2z" />
    </svg>
  )
}

function CallerForm({
  busy,
  onCreate,
}: {
  busy: boolean
  onCreate: (name: string, phone: string, address: string) => Promise<boolean>
}) {
  const [name, setName] = useState("")
  const [phone, setPhone] = useState("")
  const [address, setAddress] = useState("")

  async function submit(event: FormEvent) {
    event.preventDefault()
    const nextName = name.trim()
    const nextPhone = normalizePhone(phone)
    if (!nextName || !nextPhone) return
    if (await onCreate(nextName, nextPhone, address.trim())) {
      setName("")
      setPhone("")
      setAddress("")
    }
  }

  return (
    <form className="caller" aria-label="New caller" onSubmit={submit}>
      <label>
        Name
        <input value={name} onChange={(event) => setName(event.target.value)} />
      </label>
      <label>
        Phone
        <input
          value={phone}
          onChange={(event) => setPhone(event.target.value)}
          onBlur={() => setPhone(normalizePhone(phone))}
        />
      </label>
      <label>
        Address
        <input value={address} onChange={(event) => setAddress(event.target.value)} />
      </label>
      <button type="submit" disabled={busy}>Add caller</button>
    </form>
  )
}

function Composer({
  label,
  busy,
  onSend,
}: {
  label: string
  busy: boolean
  onSend: (body: string) => Promise<boolean>
}) {
  const [body, setBody] = useState("")

  async function submit(event: FormEvent) {
    event.preventDefault()
    const text = body.trim()
    if (!text) return
    if (await onSend(text)) setBody("")
  }

  return (
    <form className="composer" onSubmit={submit}>
      <label>
        {label}
        <textarea value={body} onChange={(event) => setBody(event.target.value)} />
      </label>
      <button type="submit" disabled={busy}>Send</button>
    </form>
  )
}

function Thread({ messages }: { messages: ChatMessage[] }) {
  return (
    <ScrollBox className="thread" watch={messages.map((message) => message.id).join()}>
      {messages.length === 0 ? <p className="empty">No messages yet.</p> : messages.map((message) => (
        <div key={message.id} className={`bubble ${message.role}`}>
          <span className="role">{message.role}</span>
          <MessageBody body={message.body} />
        </div>
      ))}
    </ScrollBox>
  )
}

function MessageBody({ body }: { body: string }) {
  return messageBlocks(body).map((block, index) => {
    if (block.kind === "p") return <p key={index}><RichText text={block.text} /></p>
    if (block.kind === "ol") {
      return (
        <ol key={index} start={block.start}>
          {block.items.map((item, itemIndex) => <li key={itemIndex}><RichText text={item} /></li>)}
        </ol>
      )
    }
    return (
      <ul key={index}>
        {block.items.map((item, itemIndex) => <li key={itemIndex}><RichText text={item} /></li>)}
      </ul>
    )
  })
}

function RichText({ text }: { text: string }) {
  const nodes: ReactNode[] = []
  const pattern = /\*\*([^*\n]+?)\*\*/g
  let last = 0
  let match: RegExpExecArray | null
  let index = 0
  while ((match = pattern.exec(text)) !== null) {
    if (match.index > last) nodes.push(text.slice(last, match.index))
    nodes.push(<strong key={index}>{match[1]}</strong>)
    index += 1
    last = match.index + match[0].length
  }
  if (last < text.length) nodes.push(text.slice(last))
  return nodes
}

function ScrollBox({
  className,
  watch,
  pin = "end",
  children,
}: {
  className: string
  watch: string
  pin?: "start" | "end"
  children: ReactNode
}) {
  const box = useRef<HTMLDivElement>(null)
  useEffect(() => {
    const node = box.current
    if (node) node.scrollTop = pin === "start" ? 0 : node.scrollHeight
  }, [watch, pin])
  return (
    <div className={className} ref={box}>
      {children}
    </div>
  )
}

function messagesFor(shop: Shop, kind: string, customerId: number | null): ChatMessage[] {
  return shop.conversations
    .filter((conversation) => conversation.kind === kind && conversation.customer_id === customerId)
    .flatMap((conversation) => conversation.messages)
    .sort((left, right) => left.id - right.id)
}

function customerName(shop: Shop, customerId: number): string {
  return shop.customers.find((customer) => customer.id === customerId)?.name ?? "Customer"
}

function technicianName(shop: Shop, technicianId: number | null): string {
  if (technicianId == null) return ""
  return shop.technicians.find((technician) => technician.id === technicianId)?.name ?? ""
}

function ordered(appointments: Appointment[]): Appointment[] {
  return [...appointments].sort((left, right) => {
    if (left.start == null && right.start == null) return right.id - left.id
    if (left.start == null) return 1
    if (right.start == null) return -1
    const byTime = right.start.localeCompare(left.start)
    return byTime === 0 ? right.id - left.id : byTime
  })
}

function newestFirst(audits: Audit[]): Audit[] {
  return [...audits].sort((left, right) => {
    const byTime = right.created_at.localeCompare(left.created_at)
    return byTime === 0 ? right.id - left.id : byTime
  })
}

function scheduleLabel(schedule: Record<string, unknown>): string {
  const weekday = typeof schedule.weekday === "string" ? schedule.weekday : ""
  const minute = typeof schedule.minute === "number" ? schedule.minute : 0
  const clock = typeof schedule.hour === "number" ? `${schedule.hour}:${String(minute).padStart(2, "0")}` : ""
  return [weekday, clock].filter(Boolean).join(" ")
}

function automationSentence(job: Shop["automations"][number]): string {
  const time = clock12(job.schedule)
  const weekday = typeof job.schedule.weekday === "string" ? titleCase(job.schedule.weekday) : ""
  if (job.kind === "morning_rundown") return `Every morning at ${time}, the owner hears who is booked.`
  if (job.kind === "appointment_reminder") {
    const when = job.schedule.day_offset === 0 ? "today's" : "tomorrow's"
    return `Every day at ${time}, customers hear about ${when} visit.`
  }
  if (job.kind === "quote_followup") {
    const quiet = typeof job.schedule.quiet_days === "number" ? job.schedule.quiet_days : 2
    const sends = typeof job.schedule.max_sends === "number" ? job.schedule.max_sends : 2
    const noun = sends === 1 ? "follow-up" : "follow-ups"
    return `Every day at ${time}, after ${quiet} days with no reply, at most ${sends} ${noun}.`
  }
  if (job.kind === "quote_report") return `Every ${weekday} at ${time}, list quotes that are still waiting.`
  return job.description ?? job.name
}

function clock12(schedule: Record<string, unknown>): string {
  const hour = typeof schedule.hour === "number" ? schedule.hour : 0
  const minute = typeof schedule.minute === "number" ? schedule.minute : 0
  const suffix = hour < 12 ? "AM" : "PM"
  const shown = hour % 12 || 12
  return `${shown}:${String(minute).padStart(2, "0")} ${suffix}`
}

function shopStatus(settings: Shop["settings"]): { open: boolean; label: string } {
  const hours = shownHours(settings)
  const closed = hours ? WEEKDAYS.filter((day) => hours[day] == null).map(titleCase) : []
  if (closed.length === 0) return { open: true, label: "Open every day" }
  return { open: false, label: `Closed ${listed(closed)}` }
}

function listed(names: string[]): string {
  if (names.length <= 1) return names[0] ?? ""
  if (names.length === 2) return `${names[0]} and ${names[1]}`
  return `${names.slice(0, -1).join(", ")}, and ${names[names.length - 1]}`
}

function policyLines(shop: Shop): string[] {
  const lines: string[] = []
  const hours = hoursPolicy(shop.settings)
  if (hours) lines.push(hours)
  const area = serviceAreaLine(shop)
  if (area) lines.push(area)
  for (const memory of shop.shop_memories) {
    if (isServiceAreaNote(memory.content)) continue
    lines.push(memory.content)
  }
  return lines
}

function isServiceAreaNote(content: string): boolean {
  return /service area/i.test(content) && /\b\d{5}\b/.test(content)
}

function serviceAreaLine(shop: Shop): string {
  const zips: string[] = []
  const area = shop.settings.service_area
  if (Array.isArray(area)) {
    for (const item of area) {
      if (typeof item === "string" && !zips.includes(item)) zips.push(item)
    }
  }
  for (const memory of shop.shop_memories) {
    if (!isServiceAreaNote(memory.content)) continue
    for (const found of memory.content.match(/\b\d{5}\b/g) ?? []) {
      if (!zips.includes(found)) zips.push(found)
    }
  }
  return zips.length > 0 ? `Service area ${zips.join(", ")}` : ""
}

function hoursPolicy(settings: Shop["settings"]): string {
  const hours = shownHours(settings)
  if (!hours) return ""
  const groups: { start: string; end: string; label: string }[] = []
  for (const day of WEEKDAYS) {
    const span = hours[day]
    const label = span == null ? "closed" : `${clockPhrase(span[0])}–${clockPhrase(span[1])}`
    const last = groups[groups.length - 1]
    if (last && last.label === label) last.end = day
    else groups.push({ start: day, end: day, label })
  }
  return groups
    .map((group) => {
      const name = group.start === group.end ? titleCase(group.start) : `${titleCase(group.start)}–${titleCase(group.end)}`
      return group.label === "closed" ? `${name} closed` : `${name} ${group.label}`
    })
    .join(". ") + "."
}

function shownHours(settings: Shop["settings"]): Record<string, [string, string] | null> | null {
  const raw = settings.hours
  if (!raw || typeof raw !== "object" || Array.isArray(raw)) return null
  const record = raw as Record<string, unknown>
  const monday = pair(record.monday) ?? ["08:00", "17:00"]
  const shown: Record<string, [string, string] | null> = {}
  for (const day of WEEKDAYS) {
    const span = pair(record[day])
    if (day === "sunday" && settings.sunday_calls === false) shown[day] = null
    else if (day === "sunday" && span == null) shown[day] = monday
    else shown[day] = span
  }
  return shown
}

function pair(value: unknown): [string, string] | null {
  if (!Array.isArray(value) || value.length !== 2) return null
  if (typeof value[0] !== "string" || typeof value[1] !== "string") return null
  return ["08:00", "17:00"]
}

function clockPhrase(value: string): string {
  const match = /^(\d{1,2}):(\d{2})$/.exec(value)
  if (!match) return value
  const hour = Number(match[1])
  const suffix = hour < 12 ? "AM" : "PM"
  return `${hour % 12 || 12}:${match[2]} ${suffix}`
}

const CAL_WEEK = ["Su", "Mo", "Tu", "We", "Th", "Fr", "Sa"]
const CAL_MONTHS = [
  "January",
  "February",
  "March",
  "April",
  "May",
  "June",
  "July",
  "August",
  "September",
  "October",
  "November",
  "December",
]

type ClockDraft = {
  year: number
  month: number
  day: number
  viewYear: number
  viewMonth: number
  hour12: number
  minute: number
  suffix: "AM" | "PM"
}

function ShopClock({
  clock,
  busy,
  onAdvance,
}: {
  clock: string
  busy: boolean
  onAdvance: (body: { until: string }) => Promise<boolean>
}) {
  const [open, setOpen] = useState(false)
  const [draft, setDraft] = useState<ClockDraft>(() => draftFrom(clock))
  const box = useRef<HTMLDivElement>(null)
  const now = shopParts(clock)
  const later = isLater(draft, now)

  useEffect(() => {
    if (!open) return
    function onPointer(event: PointerEvent) {
      if (!box.current?.contains(event.target as Node)) setOpen(false)
    }
    function onKey(event: KeyboardEvent) {
      if (event.key === "Escape") setOpen(false)
    }
    document.addEventListener("pointerdown", onPointer)
    document.addEventListener("keydown", onKey)
    return () => {
      document.removeEventListener("pointerdown", onPointer)
      document.removeEventListener("keydown", onKey)
    }
  }, [open])

  function toggle() {
    if (open) {
      setOpen(false)
      return
    }
    setDraft(draftFrom(clock))
    setOpen(true)
  }

  async function setClock() {
    if (!later) return
    const ok = await onAdvance({ until: untilValue(draft) })
    if (ok) setOpen(false)
  }

  const cells = monthCells(draft.viewYear, draft.viewMonth)
  const atCurrentMonth = draft.viewYear === now.year && draft.viewMonth === now.month

  return (
    <div className="clock-pop" ref={box}>
      <button
        type="button"
        className="clock-trigger"
        aria-expanded={open}
        aria-controls="shop-clock"
        onClick={toggle}
      >
        <span className="eyebrow">Shop clock</span>
        <time dateTime={clock}>{formatWhen(clock)}</time>
      </button>
      {open ? (
        <div id="shop-clock" className="clock-menu" role="dialog" aria-label="Move the shop clock forward">
          <p>The clock only moves forward.</p>
          <div className="month">
            <button type="button" aria-label="Previous month" disabled={busy || atCurrentMonth} onClick={() => setDraft(shiftMonth(draft, -1))}>
              ‹
            </button>
            <span>{CAL_MONTHS[draft.viewMonth - 1]} {draft.viewYear}</span>
            <button type="button" aria-label="Next month" disabled={busy} onClick={() => setDraft(shiftMonth(draft, 1))}>
              ›
            </button>
          </div>
          <div className="calendar" role="grid" aria-label="Calendar">
            {CAL_WEEK.map((name) => (
              <span key={name}>{name}</span>
            ))}
            {cells.map((day, index) =>
              day == null ? (
                <span key={`empty-${index}`} />
              ) : (
                <button
                  key={day}
                  type="button"
                  aria-label={dayLabel(draft.viewYear, draft.viewMonth, day)}
                  aria-pressed={day === draft.day && draft.viewMonth === draft.month && draft.viewYear === draft.year}
                  disabled={busy || dayStamp(draft.viewYear, draft.viewMonth, day) < dayStamp(now.year, now.month, now.day)}
                  onClick={() => setDraft({ ...draft, year: draft.viewYear, month: draft.viewMonth, day })}
                >
                  {day}
                </button>
              ),
            )}
          </div>
          <div className="face" aria-label="Clock">
            <span className="face-time">{draft.hour12}:{String(draft.minute).padStart(2, "0")} {draft.suffix}</span>
            {Array.from({ length: 12 }, (_, index) => {
              const hour = index + 1
              const angle = (hour / 12) * Math.PI * 2 - Math.PI / 2
              return (
                <button
                  key={hour}
                  type="button"
                  aria-label={`${hour} o'clock`}
                  aria-pressed={draft.hour12 === hour}
                  disabled={busy}
                  style={{ left: `${50 + 38 * Math.cos(angle)}%`, top: `${50 + 38 * Math.sin(angle)}%` }}
                  onClick={() => setDraft({ ...draft, hour12: hour })}
                >
                  {hour}
                </button>
              )
            })}
          </div>
          <div className="time-row">
            <label>
              Minute
              <select
                value={draft.minute}
                disabled={busy}
                onChange={(event) => setDraft({ ...draft, minute: Number(event.target.value) })}
              >
                {Array.from({ length: 60 }, (_, minute) => (
                  <option key={minute} value={minute}>{String(minute).padStart(2, "0")}</option>
                ))}
              </select>
            </label>
            <button type="button" aria-pressed={draft.suffix === "AM"} disabled={busy} onClick={() => setDraft({ ...draft, suffix: "AM" })}>
              AM
            </button>
            <button type="button" aria-pressed={draft.suffix === "PM"} disabled={busy} onClick={() => setDraft({ ...draft, suffix: "PM" })}>
              PM
            </button>
          </div>
          <button type="button" className="set-clock" disabled={busy || !later} onClick={() => void setClock()}>
            Set clock
          </button>
        </div>
      ) : null}
    </div>
  )
}

function draftFrom(clock: string): ClockDraft {
  const now = shopParts(clock)
  return {
    year: now.year,
    month: now.month,
    day: now.day,
    viewYear: now.year,
    viewMonth: now.month,
    hour12: now.hour % 12 || 12,
    minute: now.minute,
    suffix: now.hour >= 12 ? "PM" : "AM",
  }
}

function shopParts(iso: string) {
  const parts = new Intl.DateTimeFormat("en-US", {
    timeZone: "America/Los_Angeles",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hourCycle: "h23",
  }).formatToParts(new Date(iso))
  const bag = Object.fromEntries(parts.map((part) => [part.type, part.value]))
  return {
    year: Number(bag.year),
    month: Number(bag.month),
    day: Number(bag.day),
    hour: Number(bag.hour),
    minute: Number(bag.minute),
  }
}

function monthCells(year: number, month: number): Array<number | null> {
  const offset = new Date(Date.UTC(year, month - 1, 1)).getUTCDay()
  const count = new Date(Date.UTC(year, month, 0)).getUTCDate()
  const cells: Array<number | null> = Array.from({ length: offset }, () => null)
  for (let day = 1; day <= count; day += 1) cells.push(day)
  while (cells.length % 7 !== 0) cells.push(null)
  return cells
}

function shiftMonth(draft: ClockDraft, delta: number): ClockDraft {
  const index = draft.viewYear * 12 + (draft.viewMonth - 1) + delta
  return { ...draft, viewYear: Math.floor(index / 12), viewMonth: (index % 12) + 1 }
}

function dayStamp(year: number, month: number, day: number): number {
  return year * 10000 + month * 100 + day
}

function dayLabel(year: number, month: number, day: number): string {
  return new Intl.DateTimeFormat("en-US", {
    timeZone: "UTC",
    weekday: "long",
    month: "long",
    day: "numeric",
    year: "numeric",
  }).format(new Date(Date.UTC(year, month - 1, day)))
}

function hour24(hour12: number, suffix: "AM" | "PM"): number {
  return (hour12 % 12) + (suffix === "PM" ? 12 : 0)
}

function isLater(draft: ClockDraft, now: ReturnType<typeof shopParts>): boolean {
  const chosen = dayStamp(draft.year, draft.month, draft.day)
  const today = dayStamp(now.year, now.month, now.day)
  if (chosen !== today) return chosen > today
  return hour24(draft.hour12, draft.suffix) * 60 + draft.minute > now.hour * 60 + now.minute
}

function untilValue(draft: ClockDraft): string {
  const pad = (value: number) => String(value).padStart(2, "0")
  return `${draft.year}-${pad(draft.month)}-${pad(draft.day)}T${pad(hour24(draft.hour12, draft.suffix))}:${pad(draft.minute)}`
}

function initials(name: string): string {
  const parts = name.split(" ").filter(Boolean)
  return ((parts[0]?.[0] ?? "") + (parts.length > 1 ? parts[parts.length - 1]?.[0] ?? "" : "")).toUpperCase()
}

function formatWhen(value: string | null): string {
  if (!value) return "Unscheduled"
  const parsed = new Date(value)
  if (Number.isNaN(parsed.getTime())) return value
  return new Intl.DateTimeFormat("en-US", {
    timeZone: "America/Los_Angeles",
    weekday: "short",
    month: "short",
    day: "numeric",
    year: "numeric",
    hour: "numeric",
    minute: "2-digit",
  }).format(parsed)
}

const ACTION_LABELS: Record<string, string> = {
  book_appointment: "Booked a visit",
  complete_appointment: "Completed a visit",
  create_quote: "Sent a quote",
  set_setting: "Changed a setting",
  set_address: "Saved an address",
  update_customer: "Updated contact",
  create_customer: "Added a caller",
  remember: "Saved a note",
  open_escalation: "Escalated",
  resolve_escalation: "Resolved an escalation",
  create_automation: "Saved an automation",
  delete_automation: "Removed an automation",
  disable_automation: "Removed an automation",
  morning_rundown: "Morning rundown",
  appointment_reminder: "Appointment reminder",
  quote_followup: "Quote follow-up",
  quote_report: "Quote report",
}

const SERVICE_LABELS: Record<string, string> = {
  leak: "Leak",
  drain: "Drain",
  faucet: "Faucet",
  water_heater: "Water heater",
  water_heater_diagnostic: "Water heater",
}

const WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
const HIDDEN_FIELDS = new Set([
  "automation_id",
  "dedupe_key",
  "customer_id",
  "technician_id",
  "appointment_id",
  "escalation_id",
  "end",
])
const ORDERED_ITEM = /^\s*(\d+)[.)、]\s+/
const BULLET_ITEM = /^\s*[-*•]\s+/

function actionLabel(action: string): string {
  return ACTION_LABELS[action] ?? titleCase(action)
}

function serviceLabel(service: string): string {
  return SERVICE_LABELS[service] ?? titleCase(service)
}

function titleCase(value: string): string {
  const words = value.replaceAll("_", " ")
  return words.charAt(0).toUpperCase() + words.slice(1)
}

function activityDetail(shop: Shop, payload: Audit["payload"]): string {
  if (payload == null) return ""
  if (Array.isArray(payload)) return payload.map((item) => plainValue(item)).filter(Boolean).join(", ")
  return detailFromRecord(shop, payload)
}

function detailFromRecord(shop: Shop, payload: Record<string, unknown>): string {
  const key = typeof payload.key === "string" ? payload.key : ""
  if (key === "sunday_calls" && typeof payload.value === "boolean") {
    return payload.value ? "Sunday calls on" : "Sunday calls off"
  }
  if (key === "hours") return hoursLine(payload.value)
  if (typeof payload.date === "string" && typeof payload.visits === "number") {
    const noun = payload.visits === 1 ? "visit" : "visits"
    return `${formatDay(payload.date)} · ${payload.visits} ${noun}`
  }
  if (typeof payload.date === "string" && typeof payload.quotes === "number") {
    const noun = payload.quotes === 1 ? "quote still waiting" : "quotes still waiting"
    return `${formatDay(payload.date)} · ${payload.quotes} ${noun}`
  }
  if (typeof payload.follow_up === "number") {
    const step = typeof payload.max_sends === "number" ? `Follow-up ${payload.follow_up} of ${payload.max_sends}` : `Follow-up ${payload.follow_up}`
    const customer = customerFromAppointment(shop, payload.appointment_id)
    return customer ? `${step} · ${customer}` : step
  }

  const parts: string[] = []
  const customer = namedCustomer(shop, payload.customer_id)
  const given = typeof payload.name === "string" ? payload.name : ""
  if (customer) parts.push(customer)
  else if (given) parts.push(given)
  if (typeof payload.phone === "string") parts.push(payload.phone)
  if (typeof payload.service === "string") parts.push(serviceLabel(payload.service))
  const technician = namedTechnician(shop, payload.technician_id)
  if (technician) parts.push(technician)
  if (typeof payload.start === "string") parts.push(formatWhen(payload.start))
  if (typeof payload.price === "number") parts.push(`$${payload.price}`)
  if (typeof payload.address === "string") parts.push(payload.address)
  if (typeof payload.kind === "string" && typeof payload.content === "string") parts.push(`${payload.kind}: ${payload.content}`)
  else if (typeof payload.content === "string") parts.push(payload.content)
  if (typeof payload.detail === "string") parts.push(payload.detail)
  if (payload.schedule && typeof payload.schedule === "object" && !Array.isArray(payload.schedule)) {
    const when = prettySchedule(payload.schedule as Record<string, unknown>)
    if (when) parts.push(when)
  }
  if (parts.length > 0) return parts.join(" · ")
  if (key) {
    const shown = plainValue(payload.value)
    return shown ? `${titleCase(key)} ${shown}` : titleCase(key)
  }
  return genericDetail(payload)
}

function genericDetail(payload: Record<string, unknown>): string {
  return Object.entries(payload)
    .filter(([key]) => !HIDDEN_FIELDS.has(key))
    .map(([key, value]) => {
      const shown = plainValue(value)
      return shown ? `${titleCase(key)} ${shown}` : ""
    })
    .filter(Boolean)
    .join(" · ")
}

function plainValue(value: unknown): string {
  if (value == null || value === "") return ""
  if (typeof value === "boolean") return value ? "on" : "off"
  if (typeof value === "number") return String(value)
  if (typeof value === "string") {
    if (/^\d{4}-\d{2}-\d{2}$/.test(value)) return formatDay(value)
    if (/^\d{4}-\d{2}-\d{2}T/.test(value)) return formatWhen(value)
    return value
  }
  if (Array.isArray(value)) return value.map((item) => plainValue(item)).filter(Boolean).join(", ")
  if (typeof value === "object") {
    const hours = hoursLine(value)
    if (hours) return hours
    return Object.entries(value)
      .map(([key, item]) => {
        const shown = plainValue(item)
        return shown ? `${titleCase(key)} ${shown}` : ""
      })
      .filter(Boolean)
      .join(", ")
  }
  return ""
}

function hoursLine(value: unknown): string {
  if (!value || typeof value !== "object" || Array.isArray(value)) return ""
  const hours = value as Record<string, unknown>
  return WEEKDAYS.flatMap((day) => {
    if (!Object.prototype.hasOwnProperty.call(hours, day)) return []
    const label = titleCase(day)
    const span = hours[day]
    if (span == null) return [`${label} closed`]
    if (Array.isArray(span) && span.length === 2 && typeof span[0] === "string" && typeof span[1] === "string") {
      return [`${label} 8:00–17:00`]
    }
    return []
  }).join(", ")
}

function formatDay(value: string): string {
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value)
  if (!match) return value
  const parsed = new Date(Date.UTC(Number(match[1]), Number(match[2]) - 1, Number(match[3]), 12))
  return new Intl.DateTimeFormat("en-US", {
    timeZone: "America/Los_Angeles",
    weekday: "short",
    month: "short",
    day: "numeric",
    year: "numeric",
  }).format(parsed)
}

function namedCustomer(shop: Shop, id: unknown): string {
  if (typeof id !== "number") return ""
  return shop.customers.find((customer) => customer.id === id)?.name ?? ""
}

function namedTechnician(shop: Shop, id: unknown): string {
  if (typeof id !== "number") return ""
  return shop.technicians.find((technician) => technician.id === id)?.name ?? ""
}

function customerFromAppointment(shop: Shop, id: unknown): string {
  if (typeof id !== "number") return ""
  const job = shop.appointments.find((item) => item.id === id)
  return job ? customerName(shop, job.customer_id) : ""
}

function prettySchedule(schedule: Record<string, unknown>): string {
  const label = scheduleLabel(schedule)
  return label ? label.charAt(0).toUpperCase() + label.slice(1) : ""
}

function messageBlocks(body: string): ({ kind: "p"; text: string } | { kind: "ol"; start: number; items: string[] } | { kind: "ul"; items: string[] })[] {
  const blocks: ({ kind: "p"; text: string } | { kind: "ol"; start: number; items: string[] } | { kind: "ul"; items: string[] })[] = []
  let paragraph: string[] = []
  let list: { kind: "ol"; start: number; items: string[] } | { kind: "ul"; items: string[] } | null = null

  function flushParagraph() {
    if (paragraph.length === 0) return
    blocks.push({ kind: "p", text: paragraph.join("\n") })
    paragraph = []
  }

  function flushList() {
    if (!list) return
    blocks.push(list)
    list = null
  }

  for (const line of body.replaceAll("\r\n", "\n").split("\n")) {
    if (line.trim() === "") {
      flushParagraph()
      continue
    }
    const ordered = ORDERED_ITEM.exec(line)
    if (ordered) {
      flushParagraph()
      if (!list || list.kind !== "ol") {
        flushList()
        list = { kind: "ol", start: Number(ordered[1]), items: [] }
      }
      list.items.push(line.replace(ORDERED_ITEM, "").trim())
      continue
    }
    if (BULLET_ITEM.test(line)) {
      flushParagraph()
      if (!list || list.kind !== "ul") {
        flushList()
        list = { kind: "ul", items: [] }
      }
      list.items.push(line.replace(BULLET_ITEM, "").trim())
      continue
    }
    flushList()
    paragraph.push(line)
  }
  flushParagraph()
  flushList()
  return blocks
}

function messageOf(err: unknown): string {
  return err instanceof Error && err.message ? err.message : "request failed"
}
