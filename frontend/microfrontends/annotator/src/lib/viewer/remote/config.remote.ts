import { query } from '$app/server';
import { env } from '$env/dynamic/private';

// Zone runtime config — the HONEST-MOCK signal (the transport ruling, area 4; was `/api/config`).
// The annotator service answers assist/jobs calls with a deterministic in-repo mock until real model
// runners are deployed (the MEDIA_ASSIST_URL / MEDIA_JOBS_URL env flip, mirrored onto this web pod by
// the same deploy). This exposes only their PRESENCE (never the URLs) so the AI surfaces can label
// mocked results as mocked instead of passing them off as model output.
// Batch-job submits additionally get a per-run signal — JobResult.backend === "mock", stamped by the
// service itself — which the submit toast consumes; that field is authoritative even when this web
// pod's env drifts from the service's.
//
// No `ApiResult` union here, deliberately: there is no upstream to answer with a status. The whole
// body is a read of this pod's own private env, so the only failure mode is the RPC itself, which the
// one caller already catches to keep the mock chip up (fail-honest — see AiAssistBar.svelte).

/** The registry's producer NAMES (never the URLs — same presence-only stance as above). */
const assistProducers = (): string[] => {
	try {
		return Object.keys(JSON.parse(env.MEDIA_ASSIST_BACKENDS ?? '{}') as Record<string, unknown>);
	} catch {
		return [];
	}
};

export const zoneConfig = query(
	async (): Promise<{ assistRunner: boolean; jobsRunner: boolean; assistProducers: string[] }> => ({
		assistRunner: Boolean(env.MEDIA_ASSIST_URL) || assistProducers().length > 0,
		jobsRunner: Boolean(env.MEDIA_JOBS_URL),
		assistProducers: assistProducers(),
	}),
);
