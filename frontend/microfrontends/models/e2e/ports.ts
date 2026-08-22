/**
 * Every port this zone's e2e run binds — declared ONCE.
 *
 * The lakehouse learned this the expensive way: its numbers lived in four places (playwright.config.ts,
 * the mock server's own `Bun.serve`, a spec's fetch helper, the session helper's default origin), and
 * moving ONE of them left the other three pointing at the old number — the mock came up where nobody was
 * looking and the suite failed with ECONNREFUSED. This zone starts with the single source.
 *
 * `@rask/zone-contract` asserts no two zones bind the same port and scans this file, so a collision fails
 * a test rather than being discovered as a mystery timeout. That gate is not decorative here:
 * `reuseExistingServer` is on locally, so a port another zone already listens on is silently ADOPTED
 * rather than refused — the suite would then drive a real server and call it the mock.
 */

/** This zone's e2e dev server. AUTH-ON (OIDC env in playwright.config.ts): the registry and the
 *  experiments dashboard both read as the SIGNED-IN user, and the per-test bearer is what keeps the
 *  mock's per-bearer buckets from racing under `fullyParallel`. */
export const APP_PORT = 5298;
/** The seed-driven stand-in for every upstream this zone's remote functions reach SERVER-SIDE, where
 *  `page.route` cannot follow: the catalog (`/v1/model*`), GreptimeDB (`/v1/prometheus/*`) and the
 *  lineage probe behind the shell's bell. Their paths never collide, so CATALOG_API, GREPTIME_API and
 *  LINEAGE_API all point at this one port — the same reasoning the lakehouse's own combined
 *  observability mock is built on. NOT a zone dev-server port. */
export const MOCK_UPSTREAMS_PORT = 5284;

export const APP = `http://localhost:${APP_PORT}`;
export const MOCK_UPSTREAMS = `http://localhost:${MOCK_UPSTREAMS_PORT}`;
