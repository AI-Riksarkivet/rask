import { json } from '@sveltejs/kit';
import { env } from '$env/dynamic/private';

// Zone runtime config — the HONEST-MOCK signal. The annotator service answers assist/jobs
// calls with a deterministic in-repo mock until real model runners are deployed (the
// MEDIA_ASSIST_URL / MEDIA_JOBS_URL env flip, mirrored onto this web pod by the same
// deploy). This endpoint exposes only their PRESENCE (never the URLs) so the AI surfaces
// can label mocked results as mocked instead of passing them off as model output.
// Batch-job submits additionally get a per-run signal — JobResult.backend === "mock",
// stamped by the service itself — which the submit toast consumes; that field is
// authoritative even when this web pod's env drifts from the service's.
export const GET = () =>
	json({
		assistRunner: Boolean(env.MEDIA_ASSIST_URL),
		jobsRunner: Boolean(env.MEDIA_JOBS_URL),
	});
