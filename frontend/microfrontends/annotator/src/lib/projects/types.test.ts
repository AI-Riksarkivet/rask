/**
 * The wire contract, decoded — a backend shape change is a named failure HERE, not an
 * `undefined` three components deep. The fixtures mirror what the endpoints actually return
 * (tests/unit/test_project_read_endpoints.py + test_project_event_endpoints.py pin the server side).
 */
import { describe, expect, it } from 'vitest';
import * as v from 'valibot';

import { ProjectDetailSchema, TaskListingSchema } from './types.js';

describe('ProjectDetailSchema', () => {
	it('decodes the detail read with legal_events', () => {
		const detail = v.parse(ProjectDetailSchema, {
			project: {
				project_id: 'p1',
				tenant: 'acme',
				slug: 'vasa',
				state: 'frozen',
				publish_error: null,
				publish_progress: null,
			},
			legal_events: [
				{ event: 'archive', to: 'archived', permission: 'can_manage' },
				{ event: 'open', to: 'labeling', permission: 'can_manage' },
				{ event: 'publish', to: 'publishing', permission: 'can_publish' },
			],
		});
		expect(detail.project.state).toBe('frozen');
		expect(detail.legal_events.map((e) => e.event)).toContain('publish');
	});

	it('an unknown project state is a decode ERROR, not a silently-rendered chip', () => {
		expect(() =>
			v.parse(ProjectDetailSchema, {
				project: { project_id: 'p1', tenant: 'acme', slug: 'vasa', state: 'exploded' },
				legal_events: [],
			}),
		).toThrow();
	});
});

describe('TaskListingSchema', () => {
	it('decodes the details listing including missing ghosts', () => {
		const listing = v.parse(TaskListingSchema, {
			tasks: { t1: 'claimed' },
			counts: { claimed: 1 },
			total: 1,
			terminal: 0,
			may_publish: false,
			details: [
				{
					task_id: 't1',
					project_id: 'p1',
					state: 'claimed',
					assignee: 'gina',
					lease_expires_at: '2026-07-31T12:00:00+00:00',
					source: { kind: 'chunks', keys: ['doc/0/1'] },
					media: { kind: 'image', image_url: 's3://b/x.jpg' },
					legal_events: [{ event: 'submit', to: 'in_review', permission: 'can_annotate' }],
				},
			],
			missing: ['ghost'],
		});
		expect(listing.details?.[0]?.legal_events[0]?.event).toBe('submit');
		expect(listing.missing).toEqual(['ghost']);
	});
});
