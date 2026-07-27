import { env } from '$env/dynamic/private';
import { makeBackendProxy } from '@rask/api/bff';

// The FGA workbench's model view: GET /v1/access/model → the checked-in model.fga DSL + the live
// authorization_model_id. Estate-admin gated BY THE CATALOG; read-only by contract — model changes
// are code migrations (model.fga in the repo), never a UI write.
const CATALOG_API = env.CATALOG_API ?? 'http://localhost:2333';

export const GET = makeBackendProxy({ backendUrl: CATALOG_API, stripPrefix: /^\/capi/ });
