import type { ApiResult } from '@rask/api/client';
import { query } from '#lib/reads.svelte.ts';
import type { JetStreamOverview } from '../jetstream';

// `/admin/streams`' read. The JetStream state lives in the NATS monitor (`/jsz`), which is
// unauthenticated by design, in-cluster only, and served to no browser by any Gateway route; the
// console has no backend to read it through. So this answers 501 naming that backend, and the page,
// kept for now by the owner's decision (XC-192), renders it. The bus is read with NATS's own tools:
// the `nats` CLI, its Prometheus exporter, or Surveyor.

/** The JetStream overview the /admin/streams panel renders — not served to the console. */
export const fetchJetstreamOverview = query(async (): Promise<ApiResult<JetStreamOverview>> => ({
	ok: false,
	status: 501,
	detail:
		"the stream view needs the NATS monitor (/jsz), which no route serves to the console; read the bus with NATS's own tools",
}));
