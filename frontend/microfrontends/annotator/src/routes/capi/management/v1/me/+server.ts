import { env } from '$env/dynamic/private';
import { makeCatalogProxy } from '@rask/api/bff';

// Same-origin, GET-only pass-through to the CATALOG service, forwarding ONLY the signed-in user's
// bearer (the catalog is OIDC-only — no service-token door). Single-sourced in @rask/api/bff.
export const GET = makeCatalogProxy(env);
