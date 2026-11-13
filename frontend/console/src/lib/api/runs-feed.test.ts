/**
 * The lineage pulse follows the feed forward.
 *
 * The feed is governed per caller, so the cost of a poll is the rows it makes lineage judge. These pin
 * what each poll asks for, since that is the cost: the head once, then only what lies after the cursor.
 */

import { describe, expect, it } from 'vitest';

import { lineagePulse } from './runs-feed';

interface Page {
	events: { seq: number }[];
	next_cursor: number;
}

/**
 * A feed that answers each request from a script, recording what each one asked. The pulse never
 * ends and swallows a failed read by design, so the wait between polls throws once the script is
 * used up: a pulse that asks more than the script holds fails its test instead of spinning.
 */
function scripted(answers: (Page | 'offline' | 401)[]) {
	const asked: string[] = [];
	const fetch = async (input: string): Promise<Response> => {
		asked.push(input);
		const answer = answers.shift();
		if (answer === undefined || answer === 'offline') throw new TypeError('fetch failed');
		if (answer === 401) return new Response('{}', { status: 401 });
		return Response.json(answer);
	};
	const sleep = async (): Promise<void> => {
		if (answers.length === 0) throw new Error('the pulse polled past its script');
	};
	return { asked, pulse: lineagePulse({ lineageApi: '/api/lineage', fetch, sleep }) };
}

/** The request the pulse makes for what lies after `since`. */
const after = (since: number) => `/api/lineage/events?since=${since}&limit=1&summary=true`;

const HEAD = Number.MAX_SAFE_INTEGER;

describe('the lineage pulse', () => {
	it('settles at the feed head without reading a row, then asks only what lies after it', async () => {
		const { asked, pulse } = scripted([
			{ events: [], next_cursor: 5131 },
			// Rows the caller may not see: the cursor passes them and nothing pulses.
			{ events: [], next_cursor: 5620 },
			{ events: [{ seq: 5700 }], next_cursor: 5700 },
			{ events: [], next_cursor: 5745 },
		]);

		expect((await pulse.next()).value).toEqual({ cursor: 5131 });
		// One visible event among a burst is one pulse, at the head the burst reached.
		expect((await pulse.next()).value).toEqual({ cursor: 5745 });
		expect(asked).toEqual([HEAD, 5131, 5620, HEAD].map(after));
	});

	it('does not tick or end when a read fails, and settles on the first that answers', async () => {
		const { asked, pulse } = scripted(['offline', 401, { events: [], next_cursor: 0 }]);

		// An empty feed answers 0, which settles the consumers as any head does.
		expect((await pulse.next()).value).toEqual({ cursor: 0 });
		expect(asked).toEqual([HEAD, HEAD, HEAD].map(after));
	});

	it('keeps the visible event as its cursor when the head cannot be read after it', async () => {
		const { asked, pulse } = scripted([
			{ events: [], next_cursor: 10 },
			{ events: [{ seq: 12 }], next_cursor: 12 },
			'offline',
		]);

		await pulse.next();
		expect((await pulse.next()).value).toEqual({ cursor: 12 });
		expect(asked).toEqual([HEAD, 10, HEAD].map(after));
	});
});
