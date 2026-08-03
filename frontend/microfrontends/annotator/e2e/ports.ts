/**
 * Every port this zone's e2e run binds — declared ONCE.
 *
 * The numbers used to live inline in playwright.config.ts, which was fine while there was one server
 * and one number. The transport migration added two more (a mock upstream and a second app server),
 * and a port that drifts between the config and the thing that binds it comes up where nobody is
 * looking — `reuseExistingServer` is on locally, so playwright ADOPTS whatever already listens
 * instead of failing. `@rask/zone-contract` scans this file and asserts no two zones claim a port.
 */

/** The default app server: no model runner deployed (the stack's honest-mock state). */
export const E2E_PORT = 5299;
/** A second app server with a model runner DEPLOYED (`MEDIA_ASSIST_URL` set) — the only way to
 *  exercise the real-runner assist path, since the runner's presence is server env, not a fetch a
 *  browser can restub. */
export const RUNNER_PORT = 5298;
/** The mock ANNOTATOR service standing in for the projects/tasks/assist upstreams, which the zone's
 *  remote functions now reach SERVER-side where `page.route` cannot see them. NOT a zone dev-server
 *  port. */
export const MOCK_ANNOTATOR_PORT = 5296;

export const E2E = `http://localhost:${E2E_PORT}`;
export const RUNNER_APP = `http://localhost:${RUNNER_PORT}`;
export const MOCK_ANNOTATOR = `http://localhost:${MOCK_ANNOTATOR_PORT}`;
