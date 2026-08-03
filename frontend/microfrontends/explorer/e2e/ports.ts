/**
 * Every port this zone's e2e run binds — declared ONCE.
 *
 * `@rask/zone-contract` asserts no two zones bind the same port and scans this file (`manifest.test.ts`,
 * "no two zones bind the same port, in dev OR in e2e"). That gate exists because playwright runs with
 * `reuseExistingServer` locally: a duplicate does not fail loudly with EADDRINUSE, it silently ADOPTS
 * whatever is already listening — which is how the lakehouse admin suite once pointed its "mock catalog"
 * at this zone's real dev server.
 */

/** The zone's e2e dev server — not the 5173 dev port, so a running composition cannot clash. */
export const E2E_PORT = 5297;

/**
 * The mock media-plane services: ONE Bun server standing in for the SEARCH service and the ANNOTATOR's
 * projects plane. Their paths never collide (`/api/search` vs `/projects`), so two env vars point at one
 * port — the same trick the lakehouse suite's combined observability mock uses. NOT a zone dev-server
 * port.
 */
export const MOCK_SERVICES_PORT = 5290;

export const E2E_ORIGIN = `http://localhost:${E2E_PORT}`;
export const MOCK_SERVICES = `http://localhost:${MOCK_SERVICES_PORT}`;
