import { env } from '$env/dynamic/private';

/**
 * Absolute gateway base URL for server-side fetches (SSR loads + remote
 * functions). Overridable via `RASK_GATEWAY_URL`; defaults to the dev gateway.
 */
export const GATEWAY_URL = env.RASK_GATEWAY_URL ?? 'http://localhost:8888';
