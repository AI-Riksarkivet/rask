import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { describe, expect, it } from 'vitest';
import { REPO_ROOT } from './manifest';

/**
 * A live query has to survive the zone server's idle timeout, which the chart configures.
 *
 * `query.live` is a server-side generator that yields only when its data changes — the correct
 * discipline, and the one that makes the connection look dead to every hop in front of it. SvelteKit's
 * SSE transport sends no keepalive of its own (kit 2.70.1 `runtime/server/remote.js` has exactly one
 * `controller.enqueue`, the payload), so nothing refreshes that clock from the application side.
 * `svelte-adapter-bun` defaults Bun's `idleTimeout` to 10s, which severed every stream every ~12s. The
 * edge's own idle limit is the platform Gateway's policy (chart/values.yaml `route:`, ADR 0165).
 */

/** The shortest idle limit a common proxy defaults to (ingress-nginx's `proxy_read_timeout`). */
const PROXY_DEFAULT_READ_TIMEOUT_S = 60;
const values = readFileSync(resolve(REPO_ROOT, 'chart/values.yaml'), 'utf8');
const helpers = readFileSync(resolve(REPO_ROOT, 'chart/templates/_helpers.tpl'), 'utf8');

const idleTimeout = Number(/idleTimeoutSeconds:\s*(\d+)/.exec(values)?.[1]);

describe('a live query survives the zone server’s idle timeout', () => {
	/**
	 * `0` means DISABLED, and disabled is the strongest setting rather than the weakest — which is the
	 * opposite of what this gate first assumed. It asserted `> 60` on the raw number, so switching to 0
	 * reddened it while the estate got strictly better at holding streams. A gate whose arithmetic treats
	 * the best value as the worst teaches people to override it.
	 *
	 * The real hazard is a SMALL POSITIVE value: the adapter's own default is 10, which severed every live
	 * subscription every ~12s and made `query.live` more expensive than the `setInterval` it replaced. So
	 * the rule is: 0, or comfortably above a proxy's common default.
	 */
	const disabled = idleTimeout === 0;

	it('the zone server overrides the adapter’s 10s idle timeout', () => {
		// The env name is the adapter's, not ours: `parseInt(env("IDLE_TIMEOUT", "10"), 10)`.
		expect(helpers).toContain('name: IDLE_TIMEOUT');
		expect(disabled || idleTimeout > PROXY_DEFAULT_READ_TIMEOUT_S).toBe(true);
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
