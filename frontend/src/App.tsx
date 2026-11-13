import { useEffect, useRef, useState, type FormEvent, type ReactNode } from "react"
import { advanceClock, createCustomer, getBootstrap, resolveEscalation, sendMessage } from "./api"
import type { Appointment, ChatMessage, Shop } from "./types"

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

  const sundayOn = shop.settings.sunday_calls === true

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
          <p className="eyebrow">Shop clock</p>
          <p>
            <time dateTime={shop.clock}>{formatWhen(shop.clock)}</time>
          </p>
          <p className={sundayOn ? "pill on" : "pill off"}>Sunday calls: {sundayOn ? "on" : "off"}</p>
        </div>
      </header>
      {error ? <p className="alert" role="alert">{error}</p> : null}
      <div className="desk">
        <section className="pane" aria-label="Customer">
          {selected ? (
            <>
              <h2>{selected.name}</h2>
              {selected.address ? <p className="meta">{selected.address}</p> : null}
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
              onCreate={(name, phone) => run(async () => (await createCustomer({ name, phone })).id)}
            />
          </div>
        </section>
        <section className="pane" aria-label="Owner">
          <h2>Owner desk</h2>
          <Thread messages={ownerMessages} />
          <Composer label="Message" busy={busy} onSend={(body) => run(() => sendMessage({ audience: "owner", body }))} />
          <div className="cards">
            <section className="card">
              <h3>Clock</h3>
              <div className="clock">
                <button type="button" disabled={busy} onClick={() => void run(() => advanceClock({ to: "next_morning" }))}>
                  Next morning
                </button>
                <button type="button" disabled={busy} onClick={() => void run(() => advanceClock({ hours: 4 }))}>
                  Four hours
                </button>
                <button type="button" disabled={busy} onClick={() => void run(() => advanceClock({ to: "next_day" }))}>
                  One day
                </button>
              </div>
            </section>
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
                <ScrollBox className="log" watch={shop.audits.map((audit) => audit.id).join()}>
                  <ul className="stack">
                    {shop.audits.map((audit) => (
                      <li key={audit.id}>
                        <span className="action">{audit.action}</span>
                        {": "}
                        {audit.reasoning}
                      </li>
                    ))}
                  </ul>
                </ScrollBox>
              )}
            </section>
            <section className="card">
              <h3>Schedule</h3>
              <ul className="stack">
                {ordered(shop.appointments).map((job) => {
                  const technician = technicianName(shop, job.technician_id)
                  return (
                    <li key={job.id}>
                      <span className="who">{customerName(shop, job.customer_id)}</span>
                      {" · "}
                      {job.service}
                      {" · "}
                      <span className="status" data-status={job.status}>{job.status}</span>
                      {" · "}
                      {formatWhen(job.start)}
                      {technician ? ` · ${technician}` : ""}
                      {job.quote_amount != null ? ` · $${job.quote_amount}` : ""}
                      {" · "}
                      {job.summary}
                    </li>
                  )
                })}
              </ul>
            </section>
            <section className="card">
              <h3>Automations</h3>
              <ul className="stack">
                {shop.automations.map((job) => {
                  const when = scheduleLabel(job.schedule)
                  return (
                    <li key={job.id}>
                      <span className="who">{job.name}</span>
                      {" · "}
                      {job.kind}
                      {" · "}
                      <span className="status" data-status={job.enabled ? "on" : "off"}>{job.enabled ? "on" : "off"}</span>
                      {when ? ` · ${when}` : ""}
                    </li>
                  )
                })}
              </ul>
            </section>
            <section className="card">
              <h3>Policies</h3>
              {shop.shop_memories.length === 0 ? <p className="empty">No shop policies yet.</p> : (
                <ul className="stack">
                  {shop.shop_memories.map((memory) => (
                    <li key={memory.id}>{memory.content}</li>
                  ))}
                </ul>
              )}
            </section>
          </div>
        </section>
      </div>
    </main>
  )
}

function CallerForm({ busy, onCreate }: { busy: boolean; onCreate: (name: string, phone: string) => Promise<boolean> }) {
  const [name, setName] = useState("")
  const [phone, setPhone] = useState("")

  async function submit(event: FormEvent) {
    event.preventDefault()
    const nextName = name.trim()
    const nextPhone = phone.trim()
    if (!nextName || !nextPhone) return
    if (await onCreate(nextName, nextPhone)) {
      setName("")
      setPhone("")
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
        <input value={phone} onChange={(event) => setPhone(event.target.value)} />
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
        <p key={message.id} className={`bubble ${message.role}`}>
          <span className="role">{message.role}</span>
          {message.body}
        </p>
      ))}
    </ScrollBox>
  )
}

function ScrollBox({ className, watch, children }: { className: string; watch: string; children: ReactNode }) {
  const box = useRef<HTMLDivElement>(null)
  useEffect(() => {
    const node = box.current
    if (node) node.scrollTop = node.scrollHeight
  }, [watch])
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
  return [...appointments].sort((left, right) => (left.start ?? "9999").localeCompare(right.start ?? "9999"))
}

function scheduleLabel(schedule: Record<string, unknown>): string {
  const weekday = typeof schedule.weekday === "string" ? schedule.weekday : ""
  const minute = typeof schedule.minute === "number" ? schedule.minute : 0
  const clock = typeof schedule.hour === "number" ? `${schedule.hour}:${String(minute).padStart(2, "0")}` : ""
  return [weekday, clock].filter(Boolean).join(" ")
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
    hour: "numeric",
    minute: "2-digit",
  }).format(parsed)
}

function messageOf(err: unknown): string {
  return err instanceof Error && err.message ? err.message : "request failed"
}
