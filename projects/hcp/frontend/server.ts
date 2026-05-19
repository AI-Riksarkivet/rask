/**
 * Custom Deno server entry point.
 *
 * Wraps the @deno/svelte-adapter handler to fix the request URL protocol
 * for TLS-terminating reverse proxies (e.g. nginx ingress).
 *
 * The Deno adapter does not support adapter-node's PROTOCOL_HEADER env var,
 * so SvelteKit sees http:// while browsers send Origin: https://, causing
 * CSRF rejection on all POST requests. This wrapper reads X-Forwarded-Proto
 * and constructs a corrected Request before SvelteKit processes it.
 */
import rawDeployConfig from "./.deno-deploy/deploy.json" with { type: "json" };
import rawSvelteData from "./.deno-deploy/svelte.json" with { type: "json" };
import { prepareServer } from "./.deno-deploy/handler.ts";

const innerHandler = prepareServer(rawSvelteData, rawDeployConfig, Deno.cwd());

function fixProxyProtocol(req: Request): Request {
  const proto = req.headers.get("x-forwarded-proto");
  if (proto !== "https") return req;

  const url = new URL(req.url);
  if (url.protocol === "https:") return req;

  url.protocol = "https:";
  return new Request(url, {
    method: req.method,
    headers: req.headers,
    body: req.body,
    // @ts-ignore — required for streaming request bodies in Deno
    duplex: "half",
  });
}

Deno.serve((req, info) => innerHandler(fixProxyProtocol(req), info));
