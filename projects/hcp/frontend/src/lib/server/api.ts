import { getRequestEvent } from "$app/server";
import { error } from "@sveltejs/kit";
import { BACKEND_URL } from "$lib/server/env.js";

export async function apiFetch(
  path: string,
  init?: RequestInit,
): Promise<Response> {
  const event = getRequestEvent();
  const token = event.locals.token;
  const headers = new Headers(init?.headers);
  if (token) {
    headers.set("Authorization", `Bearer ${token}`);
  }
  const url = `${BACKEND_URL}${path}`;
  try {
    const res = await fetch(url, { ...init, headers });
    if (!res.ok) {
      console.warn(
        `[apiFetch] ${init?.method ?? "GET"} ${url} → ${res.status}`,
      );
    }
    return res;
  } catch (err) {
    const msg = err instanceof Error ? err.message : "unknown error";
    console.error(
      `[apiFetch] ${init?.method ?? "GET"} ${url} failed: ${msg}`,
      `(BACKEND_URL=${BACKEND_URL})`,
    );
    error(502, `Backend unreachable: ${msg}`);
  }
}

export async function throwIfNotOk(
  res: Response,
  fallback: string,
): Promise<void> {
  if (!res.ok) {
    // Read body as text first (body can only be consumed once)
    const text = await res.text().catch(() => "");
    let detail: string;
    try {
      const err = JSON.parse(text);
      detail = typeof err.detail === "string"
        ? err.detail
        : typeof err.message === "string"
          ? err.message
          : JSON.stringify(err.detail ?? err);
    } catch {
      detail = text
        ? `${fallback} (HTTP ${res.status}: ${text.slice(0, 200)})`
        : `${fallback} (HTTP ${res.status})`;
      console.error(
        `[throwIfNotOk] Non-JSON error response: ${res.status} ${res.statusText}`,
        text.slice(0, 500),
      );
    }
    error(res.status, detail);
  }
}
