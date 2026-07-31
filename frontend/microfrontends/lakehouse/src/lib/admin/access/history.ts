// Recent queries, so "what did I just ask?" is answerable.
//
// The URL already makes any ONE query shareable, which is the better primitive for handing a finding to
// someone else. It is the wrong primitive for retracing your own steps: an investigation is a sequence,
// and `replaceState` (deliberately, so the address bar does not fill with keystrokes) means the back
// button cannot walk it. This is that sequence.
//
// STORAGE RULE: nothing here is not already in the URL. Every entry is a query string that
// `Explorer.pushUrl` produced, so the history can disclose nothing the address bar did not. That matters
// because this is an estate-admin surface — subject ids and object ids are exactly the sensitive part —
// and localStorage is readable by any script on the origin. Verdicts, tuples and derivations are
// deliberately NOT stored: those are answers from the store, and answers do not belong in a bookmark.

const KEY = 'rask.access.history.v1';

/** Bounded, because an unbounded list is a slow leak of an admin's whole session into localStorage and
 *  a scroll nobody reads. Twelve is roughly one investigation's worth. */
const MAX = 12;

export type HistoryEntry = {
	/** The exact query string, as `pushUrl` wrote it — replayed verbatim, never re-derived. */
	query: string;
	/** A short human label, computed at write time from the query's own parameters. */
	label: string;
	/** Epoch ms. Only used for ordering and a relative timestamp; never sent anywhere. */
	at: number;
};

/** `localStorage` throws in a few real situations — Safari private mode, a storage-partitioned iframe,
 *  a user who disabled site data. History is a convenience, so every path here degrades to "no history"
 *  rather than taking the page down with it. */
function storage(): Storage | null {
	try {
		if (typeof localStorage === 'undefined') return null;
		return localStorage;
	} catch {
		return null;
	}
}

/** A readable one-liner for a query, derived from the query itself so it can never describe a
 *  different search than the one it replays. */
export function describeQuery(query: string): string {
	const q = new URLSearchParams(query);
	const kind = q.get('q') ?? 'why';
	const user = q.get('user') ?? '';
	const relation = q.get('relation') ?? '';
	const object = q.get('object') ?? '';
	const type = q.get('type') ?? '';
	if (q.get('view') === 'model') return 'model schema';
	if (kind === 'what') return `what can ${user || '?'} do (${relation || '?'} on ${type || '?'})`;
	if (kind === 'who') return `who can ${relation || '?'} ${object || '?'}`;
	return `why ${user || '?'} ${relation || '?'} ${object || '?'}`;
}

export function loadHistory(): HistoryEntry[] {
	const store = storage();
	if (!store) return [];
	try {
		const raw = store.getItem(KEY);
		if (!raw) return [];
		const parsed: unknown = JSON.parse(raw);
		if (!Array.isArray(parsed)) return [];
		// Shape-checked rather than trusted: localStorage is writable by anything on the origin, and a
		// malformed entry rendering as `undefined` in a list is a worse failure than an empty history.
		return parsed
			.filter(
				(e): e is HistoryEntry =>
					!!e &&
					typeof e === 'object' &&
					typeof (e as HistoryEntry).query === 'string' &&
					typeof (e as HistoryEntry).label === 'string' &&
					typeof (e as HistoryEntry).at === 'number',
			)
			.slice(0, MAX);
	} catch {
		return [];
	}
}

/**
 * Record a query, most-recent-first.
 *
 * De-duplicated on the query string, and the duplicate MOVES rather than stacking — asking the same
 * question twice is one entry that got more recent, not two entries. Returns the new list so the caller
 * can hold it in state without re-reading storage.
 */
export function recordQuery(query: string): HistoryEntry[] {
	// A query with nothing in it is the resting state, not a search worth remembering.
	const meaningful = new URLSearchParams(query);
	meaningful.delete('run');
	if ([...meaningful].every(([, v]) => !v)) return loadHistory();

	const entry: HistoryEntry = { query, label: describeQuery(query), at: Date.now() };
	// De-duplicated on the LABEL, not the raw query string — the label is the question, the query string
	// also carries view state. Running the same search twice moves `seed` (it is set from the answer),
	// so a string comparison filed one question as two entries that read identically in the list.
	const next = [entry, ...loadHistory().filter((e) => e.label !== entry.label)].slice(0, MAX);
	const store = storage();
	try {
		store?.setItem(KEY, JSON.stringify(next));
	} catch {
		// Quota exceeded, or storage denied mid-session. The in-memory list is still correct for this
		// page; losing persistence is not worth an error surface on an admin's screen.
	}
	return next;
}

export function clearHistory(): HistoryEntry[] {
	try {
		storage()?.removeItem(KEY);
	} catch {
		/* see recordQuery */
	}
	return [];
}
