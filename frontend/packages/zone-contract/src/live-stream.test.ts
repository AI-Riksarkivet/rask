import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { describe, expect, it } from 'vitest';
import { REPO_ROOT } from './manifest';

/**
 * A live query has to survive TWO idle timeouts, and the estate configures both in the same chart.
 *
 * `query.live` is a server-side generator that yields only when its data changes — the correct
 * discipline, and the one that makes the connection look dead to every hop in front of it. SvelteKit's
 * SSE transport sends no keepalive of its own (kit 2.70.1 `runtime/server/remote.js` has exactly one
 * `controller.enqueue`, the payload), so nothing refreshes those clocks from the application side.
 *
 * Both hops were wrong, and only one was suspected. ingress-nginx defaults `proxy_read_timeout` to 60s,
 * and `svelte-adapter-bun` defaults Bun's `idleTimeout` to 10s — so on kind the streams were dying every
 * ~12s with "upstream prematurely closed connection" in the nginx log while the annotation everyone was
 * looking at said 3600. Raising one without the other buys nothing: the smaller wins.
 *
 * This gate is why the numbers cannot drift apart again. It reads the chart, not a comment.
 */

const NGINX_DEFAULT_READ_TIMEOUT_S = 60;
const values = readFileSync(resolve(REPO_ROOT, 'chart/values.yaml'), 'utf8');
const helpers = readFileSync(resolve(REPO_ROOT, 'chart/templates/_helpers.tpl'), 'utf8');

const proxyReadTimeout = Number(
	/nginx\.ingress\.kubernetes\.io\/proxy-read-timeout:\s*"?(\d+)"?/.exec(values)?.[1],
);
const idleTimeout = Number(/idleTimeoutSeconds:\s*(\d+)/.exec(values)?.[1]);

describe('a live query survives both idle timeouts', () => {
	it('the Ingress overrides nginx’s 60s proxy-read-timeout', () => {
		expect(proxyReadTimeout).toBeGreaterThan(NGINX_DEFAULT_READ_TIMEOUT_S);
	});

	/**
	 * `0` means DISABLED, and disabled is the strongest setting rather than the weakest — which is the
	 * opposite of what this gate first assumed. It asserted `> 60` on the raw number, so switching to 0
	 * reddened it while the estate got strictly better at holding streams. A gate whose arithmetic treats
	 * the best value as the worst teaches people to override it.
	 *
	 * The real hazard is a SMALL POSITIVE value: the adapter's own default is 10, which severed every live
	 * subscription every ~12s and made `query.live` more expensive than the `setInterval` it replaced. So
	 * the rule is: 0, or comfortably above nginx's default.
	 */
	const disabled = idleTimeout === 0;

	it('the zone server overrides the adapter’s 10s idle timeout', () => {
		// The env name is the adapter's, not ours: `parseInt(env("IDLE_TIMEOUT", "10"), 10)`.
		expect(helpers).toContain('name: IDLE_TIMEOUT');
		expect(disabled || idleTimeout > NGINX_DEFAULT_READ_TIMEOUT_S).toBe(true);
	});

	it('the zone server’s timeout is not the binding constraint', () => {
		// The proxy may hold longer than the origin only if the origin is not the one cutting first.
		// Whichever is smaller is the real lifetime of every live subscription in the estate — unless the
		// origin has no limit at all, in which case the proxy is the only clock.
		const binding = disabled ? proxyReadTimeout : Math.min(proxyReadTimeout, idleTimeout);
		expect(binding).toBeGreaterThan(NGINX_DEFAULT_READ_TIMEOUT_S);
	});

	it('the helper does not run IDLE_TIMEOUT through Helm’s `default`', () => {
		// This gate reads values.yaml, so it cannot see how the value is RENDERED — and that is exactly
		// where it went wrong. `{{ .Values.frontend.idleTimeoutSeconds | default 255 }}` renders 255 for an
		// explicit 0, because Helm's `default` treats 0 as empty. values.yaml said 0, the pod got 255, and
		// the change looked applied while nothing moved. The same trap is already recorded for booleans.
		const line = /name: IDLE_TIMEOUT[^\n]*/.exec(helpers)?.[0] ?? '';
		expect(line).not.toContain('| default');
		expect(line).toContain('hasKey');
	});

	it('IDLE_TIMEOUT is either disabled or within Bun’s ceiling', () => {
		// Bun.serve rejects an idleTimeout above 255. Measured 2026-07-26: even AT 255, with a 20s
		// application keepalive already writing to the socket, a held stream died at 256.8s — so the
		// timeout is a maximum connection LIFETIME for a streaming response, not an idle timer, and no
		// keepalive can beat it. Hence 0. The wedged-client objection is answered by that same keepalive:
		// the write fails when the peer is gone and the generator ends.
		expect(idleTimeout).toBeGreaterThanOrEqual(0);
		expect(idleTimeout).toBeLessThanOrEqual(255);
	});
});
