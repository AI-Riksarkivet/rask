#!/usr/bin/env bash
# Browse the LIVE rask estate running in the local kind cluster.
#
# kind maps no host ports and no ingress controller runs there, so the k8s Ingress
# (which is what composes the zones in a real deploy) has no address. This script
# reproduces that edge faithfully: one port-forward per zone + the gateway, and a tiny
# proxy on :3024 carrying the SAME longest-prefix path table the Ingress declares.
#
#   ./scripts/kind-browse.sh          → http://localhost:3024
#
# Ctrl-C tears every forward down. Zone forwards live in the 92xx range so they cannot collide
# with `make dev-frontends` (5173-5273) or a local fleet (:8888).
#
# THE PROXY PORT IS 3124, NOT 3024. It used to be 3024 — which is exactly the port
# `make dev-frontends` binds for the Turborepo composition proxy. Two different tools claimed one
# port, and the failure was silent in the worst direction: whichever started second either died or
# was adopted, so you could believe you were browsing the CLUSTER while looking at local dev
# servers, or the reverse. The comment above used to say the ports "cannot collide" — true of the
# zone forwards, false of the proxy, which is the one you actually open. Override with PORT=.
set -euo pipefail

CLUSTER="${KIND_CLUSTER:-rask}"
CTX="kind-${CLUSTER}"
KUBECTL="${KUBECTL:-$(dirname "$0")/../.localbin/kubectl}"
[ -x "$KUBECTL" ] || KUBECTL=kubectl
PORT="${PORT:-3124}"

# Refuse a busy port rather than racing for it. `bun` would otherwise either fail with a stack
# trace or, worse, silently serve nothing while an unrelated process answers on that port.
if command -v ss >/dev/null && ss -ltn 2>/dev/null | grep -q ":${PORT} "; then
  echo "!! port ${PORT} is already in use — refusing to start."
  echo "   Something else is bound there (\`make dev-frontends\` uses :3024). Free it, or: PORT=3125 $0"
  exit 1
fi

command -v bun >/dev/null || { echo "!! bun not on PATH (needed for the composing proxy)"; exit 1; }
"$KUBECTL" --context "$CTX" get ns >/dev/null 2>&1 || {
  echo "!! cluster '$CTX' unreachable. Bring it up with: make kind-up kind-preload kind-images kind-load kind-deploy"; exit 1; }

# zone → local port. home is the catch-all and owns '/'.
# Must match `git ls-files frontend/microfrontends | cut -d/ -f3 | sort -u`. It did NOT: this list
# carried `media` and `train` — neither of which has a single tracked file — and omitted `explorer`
# and `models`, which are real. So the browser port-forwarded two services that do not exist and
# offered no way to reach two that do. Same roster drift #71 found in CLAUDE.md and the skill.
declare -A ZONE_PORT=(
  [home]=9273 [lakehouse]=9274 [explorer]=9279 [annotator]=9277 [compute]=9275 [studio]=9276 [models]=9278
)

PIDS=()
cleanup() { echo; echo "==> tearing down port-forwards"; for p in "${PIDS[@]:-}"; do kill -- "-$p" 2>/dev/null || kill "$p" 2>/dev/null || true; done; pkill -P $$ 2>/dev/null || true; }
trap cleanup EXIT INT TERM

fwd() { # fwd <svc> <local> <remote> — RESPAWNING: kubectl port-forward dies on idle or pod restart,
        # and a silently dead forward is indistinguishable from a broken backend.
  ( while true; do "$KUBECTL" --context "$CTX" port-forward "svc/$1" "$2:$3" >/dev/null 2>&1; sleep 2; done ) &
  PIDS+=($!)
}

echo "==> forwarding the gateway and all seven zones"
fwd rask-gateway 9888 8888
for z in "${!ZONE_PORT[@]}"; do fwd "rask-web-$z" "${ZONE_PORT[$z]}" 3000; done
sleep 4

PROXY="$(mktemp /tmp/rask-edge-XXXX.ts)"
cat > "$PROXY" <<'TS'
// The Ingress path table, longest-prefix-first. Everything else falls through to home.
const ZONES: [string, number][] = [
	['/lakehouse', 9274], ['/annotator', 9277], ['/compute', 9275],
	['/studio', 9276], ['/train', 9278], ['/explorer', 9279],
];
const GATEWAY = 9888, HOME = 9273;
const port = Number(process.env.PORT ?? 3024);

Bun.serve({
	port,
	idleTimeout: 120,
	async fetch(req) {
		const url = new URL(req.url);
		let target = HOME;
		if (url.pathname.startsWith('/api')) target = GATEWAY;
		else for (const [prefix, p] of ZONES) {
			if (url.pathname === prefix || url.pathname.startsWith(prefix + '/')) { target = p; break; }
		}
		const upstream = new URL(url.pathname + url.search, `http://localhost:${target}`);
		let res: Response;
		try {
			res = await fetch(upstream, {
			method: req.method,
			headers: req.headers,
			body: req.body,
			redirect: 'manual',
			// @ts-expect-error bun streaming bodies
				duplex: 'half',
			});
		} catch (err) {
			// A dead `kubectl port-forward` must be UNMISTAKABLE. Without this the fetch throws,
			// Bun renders its own HTML error page, and a tunnel user sees a 500 that looks exactly
			// like a backend fault — which cost a real debugging detour on 2026-07-28.
			const msg = `edge-proxy: upstream localhost:${target} is unreachable (the kubectl port-forward for this route died). Restart ./scripts/kind-browse.sh`;
			console.error('!! ' + msg);
			return new Response(msg, { status: 502, headers: { 'content-type': 'text/plain' } });
		}
		// Drop the encoding header — the body is already decoded by fetch, and a stale
		// content-encoding makes the browser fail with ERR_CONTENT_DECODING_FAILED.
		const headers = new Headers(res.headers);
		headers.delete('content-encoding');
		headers.delete('content-length');
		return new Response(res.body, { status: res.status, headers });
	},
});
console.log(`\n  rask is live →  http://localhost:${port}\n`);
TS

echo "==> composing on :$PORT (Ctrl-C to stop)"
PORT="$PORT" bun "$PROXY"
