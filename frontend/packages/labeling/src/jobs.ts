/**
 * The batch-labeling WIRE CONTRACT — the shape `/api/jobs/apply` answers with.
 *
 * The fetch transport that used to live beside it (`submitBatchJob`/`jobStatus`) is DELETED (#94):
 * both zones that speak this wire moved onto remote functions in the transport ruling (annotator's
 * `jobs.remote.ts`, explorer's `labeling.remote.ts`), no caller was left, and a dead transport with
 * an `as JobResult` cast is exactly the drift a shared seam exists to prevent. The contract stays
 * here so both remotes keep importing ONE `JobResult` rather than re-declaring it.
 */

export interface JobResult {
	job_id: string;
	status: string; // queued | running | succeeded | failed
	backend: string; // "mock" | "remote"
}
