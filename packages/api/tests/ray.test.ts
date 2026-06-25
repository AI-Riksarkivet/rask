import { describe, it, expect } from 'vitest';
import { parse } from '../src/parse.js';
import { RayHealthSchema, RayJobsPayloadSchema, RayClusterPayloadSchema } from '../src/ray.js';

// The ray-api endpoints serialize their `error` field as JSON `null` in the
// healthy case (FastAPI renders `Optional[str]` as `null`, not omitted). The
// payload schemas must therefore accept `null` — see parse.ts: "every such
// field is a v.nullable(...)". A regression here 500s every compute view that
// reads these payloads (jobs, cluster, overview/health).

describe('ray payload schemas accept error: null (healthy case)', () => {
	it('RayJobsPayload parses with error: null', () => {
		const payload = { ok: true, dashboard_url: 'http://ray:8265', jobs: [], error: null };
		expect(() => parse(RayJobsPayloadSchema, payload)).not.toThrow();
		expect(parse(RayJobsPayloadSchema, payload).error).toBeNull();
	});

	it('RayClusterPayload parses with error: null', () => {
		const payload = {
			ok: true,
			dashboard_url: 'http://ray:8265',
			node_count: 1,
			alive_count: 1,
			nodes: [],
			error: null,
		};
		expect(() => parse(RayClusterPayloadSchema, payload)).not.toThrow();
		expect(parse(RayClusterPayloadSchema, payload).error).toBeNull();
	});

	it('RayHealth parses with error: null', () => {
		const payload = { ok: true, dashboard_url: 'http://ray:8265', error: null };
		expect(() => parse(RayHealthSchema, payload)).not.toThrow();
	});
});
