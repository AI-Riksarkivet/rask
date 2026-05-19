import type { RequestHandler } from "./$types.js";
import { BACKEND_URL } from "$lib/server/env.js";

const handler: RequestHandler = async ({ params, request, locals }) => {
  const url = new URL(request.url);
  const target = `${BACKEND_URL}/api/${params.path}${url.search}`;

  const headers = new Headers();
  const contentType = request.headers.get("content-type");
  if (contentType) {
    headers.set("content-type", contentType);
  }
  // Forward explicit Authorization header (REST clients, Swagger "Try it out"),
  // or inject Bearer token from the httpOnly session cookie (browser requests).
  const clientAuth = request.headers.get("authorization");
  if (clientAuth) {
    headers.set("authorization", clientAuth);
  } else if (locals.token) {
    headers.set("authorization", `Bearer ${locals.token}`);
  }

  let response: Response;
  try {
    response = await fetch(target, {
      method: request.method,
      headers,
      body: request.method !== "GET" && request.method !== "HEAD"
        ? request.body
        : undefined,
      // @ts-expect-error duplex is needed for streaming request bodies
      duplex: "half",
    });
  } catch (err) {
    const msg = err instanceof Error ? err.message : "unknown error";
    console.error(`[proxy] ${request.method} ${target} failed: ${msg}`);
    return new Response(
      JSON.stringify({ detail: "Backend service unavailable" }),
      { status: 502, headers: { "content-type": "application/json" } },
    );
  }

  if (!response.ok) {
    console.warn(
      `[proxy] ${request.method} ${target} → ${response.status}`,
    );
  }

  const responseHeaders: Record<string, string> = {
    "content-type": response.headers.get("content-type") ??
      "application/json",
  };
  const contentLength = response.headers.get("content-length");
  if (contentLength) {
    responseHeaders["content-length"] = contentLength;
  }
  const contentDisposition = response.headers.get("content-disposition");
  if (contentDisposition) {
    responseHeaders["content-disposition"] = contentDisposition;
  }

  return new Response(response.body, {
    status: response.status,
    statusText: response.statusText,
    headers: responseHeaders,
  });
};

export const GET = handler;
export const POST = handler;
export const PUT = handler;
export const DELETE = handler;
export const HEAD = handler;
export const PATCH = handler;
