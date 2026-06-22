import { env } from '$env/dynamic/private';

/**
 * Absolute gateway base URL for **server-side** fetches (SSR `load` + remote
 * functions). The browser uses the relative `/api/*` Vite/proxy path, but code
 * running on the SvelteKit Bun server has no origin, so it must target the
 * gateway directly. Overridable via `RASK_GATEWAY_URL` (matches the repo's
 * `RASK_*` env convention); defaults to the dev gateway on :8888.
 */
export const GATEWAY_URL = env.RASK_GATEWAY_URL ?? 'http://localhost:8888';
