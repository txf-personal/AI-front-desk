import type { Customer, Shop } from "./types"

export function getBootstrap(): Promise<Shop> {
  return request<Shop>("/api/bootstrap")
}

export function sendMessage(body: {
  audience: "customer" | "owner"
  customer_id?: number
  body: string
}): Promise<{ reply: string; conversation_id: number }> {
  return request("/api/messages", { method: "POST", body: JSON.stringify(body) })
}

export function createCustomer(body: { name: string; phone: string; address?: string }): Promise<Customer> {
  return request("/api/customers", { method: "POST", body: JSON.stringify(body) })
}

export function updateCustomer(
  id: number,
  body: { phone: string; address: string | null },
): Promise<Customer> {
  return request(`/api/customers/${id}`, { method: "PATCH", body: JSON.stringify(body) })
}

export function advanceClock(
  body: { hours: number } | { to: "next_morning" | "next_day" } | { until: string },
): Promise<{ clock: string }> {
  return request("/api/clock/advance", { method: "POST", body: JSON.stringify(body) })
}

export function resolveEscalation(id: number): Promise<{ id: number; status: string }> {
  return request(`/api/escalations/${id}/resolve`, { method: "POST", body: "{}" })
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(path, {
    ...init,
    headers: {
      Accept: "application/json",
      ...(init?.body ? { "Content-Type": "application/json" } : {}),
      ...init?.headers,
    },
  })
  const payload = await response.json().catch(() => null)
  if (!response.ok) {
    throw new Error(errorDetail(payload))
  }
  return payload as T
}

function errorDetail(payload: unknown): string {
  if (!payload || typeof payload !== "object" || !("detail" in payload)) return "request failed"
  const detail = payload.detail
  if (typeof detail === "string" && detail.trim()) return detail
  if (Array.isArray(detail)) {
    const parts = detail.flatMap((item) => {
      if (typeof item === "string" && item.trim()) return [item.trim()]
      if (item && typeof item === "object" && "msg" in item && typeof item.msg === "string" && item.msg.trim()) {
        return [item.msg.trim()]
      }
      return []
    })
    if (parts.length > 0) return parts.join(" ")
  }
  return "request failed"
}
