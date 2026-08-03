/**
 * Every port this zone's e2e run binds — declared ONCE.
 *
 * There were four copies of these numbers (playwright.config.ts, the mock catalog's own Bun.serve,
 * the control-events spec's fetch helper, session.ts's default origin). Moving the mock catalog off
 * a port the media zone also claimed changed one of them, and the other three kept pointing at the
 * old number: the mock came up where nobody was looking and the suite failed with ECONNREFUSED.
 *
 * `@rask/zone-contract` asserts no two zones bind the same port and scans this file, so a collision
 * fails a test rather than being discovered as a mystery timeout.
 */

/** The auth-OFF dev server: the data, lineage and models areas. */
export const AUTH_OFF_PORT = 5294;
/** The auth-ON dev server: the admin area, which exercises the real login gate. */
export const AUTH_ON_PORT = 5295;
/** The mock catalog backing the admin area's control-event feed. NOT a zone dev-server port. */
export const MOCK_CATALOG_PORT = 5292;
/** The mock LINEAGE service backing the live cursor + the shell's run feed, which both run
 *  server-side and so cannot be reached by `page.route`. NOT a zone dev-server port. */
export const MOCK_LINEAGE_PORT = 5291;

export const AUTH_OFF = `http://localhost:${AUTH_OFF_PORT}`;
export const AUTH_ON = `http://localhost:${AUTH_ON_PORT}`;
export const MOCK_CATALOG = `http://localhost:${MOCK_CATALOG_PORT}`;
export const MOCK_LINEAGE = `http://localhost:${MOCK_LINEAGE_PORT}`;

/** The combined observability/pipeline mock: one server standing in for GreptimeDB (`/v1/sql`,
 *  `/v1/promql*`), the NATS monitor (`/jsz`) and the medallion head (`/produce`, `/train`) — their
 *  paths never collide, so three env vars point at one port. Purely seed-driven (see the file). */
export const MOCK_OBS_PORT = 5289;
export const MOCK_OBS = `http://localhost:${MOCK_OBS_PORT}`;
