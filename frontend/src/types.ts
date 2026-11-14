export type Memory = {
  id: number
  kind: string
  content: string
}

export type Customer = {
  id: number
  name: string
  phone: string
  email: string | null
  address: string | null
  memories: Memory[]
}

export type Appointment = {
  id: number
  customer_id: number
  technician_id: number | null
  service: string
  summary: string
  status: string
  start: string | null
  end: string | null
  quote_amount: number | null
}

export type Automation = {
  id: number
  name: string
  kind: string
  enabled: boolean
  schedule: Record<string, unknown>
}

export type Escalation = {
  id: number
  customer_id: number | null
  reason: string
  detail: string
  status: string
  created_at: string
  resolved_at: string | null
}

export type Audit = {
  id: number
  actor: string
  action: string
  reasoning: string
  payload: Record<string, unknown> | unknown[] | null
  created_at: string
}

export type ChatMessage = {
  id: number
  role: string
  body: string
  created_at: string
}

export type Conversation = {
  id: number
  customer_id: number | null
  kind: string
  messages: ChatMessage[]
}

export type Shop = {
  business: {
    id: number
    name: string
    owner_name: string
    timezone: string
    phone: string
  }
  clock: string
  settings: Record<string, unknown>
  shop_memories: Memory[]
  technicians: { id: number; name: string; skills: string[] }[]
  customers: Customer[]
  appointments: Appointment[]
  automations: Automation[]
  escalations: Escalation[]
  audits: Audit[]
  conversations: Conversation[]
}
