import { env } from '$env/dynamic/private';
import { proxyServeInfer } from '@rask/api/serve-proxy';
import type { RequestHandler } from './$types';

/**
 * HTR INFERENCE — raw image bytes in, ALTO XML out.
 *
 * The body is `proxyServeInfer` in `@rask/api/serve-proxy`: the auth/size/timeout guards, the failure
 * taxonomy (`upstream_unreachable` 503 / `wrong_serve_app` 501 / `upstream_error` 502), and the full
 * reasoning for why inference talks to the Ray Serve ingress DIRECTLY rather than through the gateway
 * all live in that helper's docstring. The studio zone mounts the same helper, so the two zones'
 * inference doors cannot drift apart.
 *
 * The ONE thing that stays here is the upstream, because `$env/dynamic/private` is a SvelteKit virtual
 * module and @rask/api stays env-free: `MODELS_SERVE_URL`, default `http://localhost:8000/htrflow`.
 * Note that plain `make serve-up` deploys `transcribe` ONLY, which has no HTTP handler — so the honest
 * first-run answer is the helper's `wrong_serve_app` 501, and `make serve-up-htrflow` (or
 * `serve-up-both`) is what makes this work.
 */
const SERVE_URL = env.MODELS_SERVE_URL ?? 'http://localhost:8000/htrflow';

export const POST: RequestHandler = (event) => proxyServeInfer(event, { target: SERVE_URL });
