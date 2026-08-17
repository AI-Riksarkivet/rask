/**
 * The Ray job payload contract, and the one field that took the operator's whole view of the cluster
 * down.
 *
 * `metadata` was required. Ray OMITS the key entirely when the submitter set none — and only the
 * medallion's own submit path sets it (`rask.originator`, `rask.project`). So a job submitted by a
 * person at the CLI, by another tool, or by a workload that opted out made `getRayJobs` throw
 * `ValiError: Invalid key: Expected "metadata" but received undefined`, and `/compute/jobs` answered
 * 500 with `{"message":"Internal Error"}`.
 *
 * Measured against the live cluster on 2026-08-17: every `htr_http-chunk-*` job returned no
 * `metadata` key. The board was therefore not degraded, it was blank — and the error named a field
 * rather than the job that carried it, so the fault read as a frontend bug rather than a payload one.
 *
 * The lesson these tests pin is the general one: a list payload is parsed as ONE document, so an
 * over-strict field on a single row is not a row-level defect. It is a total outage of the view.
 */

import { describe, expect, it } from 'vitest';
import * as v from 'valibot';

import { RayJobSchema, RayJobsPayloadSchema } from './ray';

/** The shape Ray actually returns for a job submitted with no metadata. */
const JOB_WITHOUT_METADATA = {
	submission_id: 'raysubmit_9mNP5arxfbMMX5Pt',
	job_id: '01000000',
	status: 'SUCCEEDED',
	entrypoint: 'python /home/ray/jobs/ray_dummy_job.py',
	batches: [],
	start_time: 1_755_000_000_000,
	end_time: 1_755_000_060_000,
	message: null,
	error_type: null,
	driver_exit_code: 0,
	logs_url: null,
};

describe('RayJobSchema', () => {
	it('accepts a job Ray returned with NO metadata key', () => {
		const parsed = v.parse(RayJobSchema, JOB_WITHOUT_METADATA);
		expect(parsed.submission_id).toBe('raysubmit_9mNP5arxfbMMX5Pt');
	});

	it('defaults absent metadata to an empty object so callers read it unconditionally', () => {
		// The distinction between "no metadata" and "empty metadata" is not one any caller acts on,
		// and leaving it undefined pushes an optional-chain into every consumer instead.
		expect(v.parse(RayJobSchema, JOB_WITHOUT_METADATA).metadata).toEqual({});
	});

	it('still carries metadata when the submitter set it — the medallion path', () => {
		const parsed = v.parse(RayJobSchema, {
			...JOB_WITHOUT_METADATA,
			metadata: { 'rask.originator': 'alice', 'rask.project': 'acme' },
		});
		expect(parsed.metadata['rask.originator']).toBe('alice');
	});
});

describe('RayJobsPayloadSchema', () => {
	it('ONE metadata-less job does not blank the whole board', () => {
		// The regression, stated as the operator experiences it. A list is parsed as one document, so
		// before the fix this threw and /compute/jobs answered 500 — no jobs at all, not one row short.
		const payload = v.parse(RayJobsPayloadSchema, {
			ok: true,
			dashboard_url: 'http://rask-ray-head-svc:8265',
			jobs: [
				{ ...JOB_WITHOUT_METADATA, metadata: { 'rask.project': 'acme' } },
				JOB_WITHOUT_METADATA,
			],
		});
		expect(payload.jobs).toHaveLength(2);
		expect(payload.jobs?.[1]?.metadata).toEqual({});
	});
});
