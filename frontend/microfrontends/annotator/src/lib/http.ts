import { base } from '$app/paths';
import { createBffClient } from '@rask/api/client';

// This zone's binding of the shared browser-side BFF client (@rask/api/client): the ONLY per-zone
// variable is the SvelteKit base path this zone is served under, so the helpers are single-sourced and
// bound here once. Re-exported by name so call sites keep importing `$lib/http`.
const bff = createBffClient(base);
export const { fetchMeViaBff } = bff;
