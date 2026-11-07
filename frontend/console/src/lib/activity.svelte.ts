/**
 * The reader's own activity: the outcome of every write this console sent for the signed-in person,
 * newest first. It is what the navbar's bell lists, and it is kept in the browser alone, as
 * Lakekeeper's console keeps its notifications (`console-components` `src/stores/notifications.ts`):
 * nothing here is read from or sent to a service.
 *
 * Every write declared with `command` (`reads.svelte.ts`) records here, a success or a failure with its
 * HTTP status and detail. Reads record nothing: a read's failure renders on the page it belongs to.
 *
 * Where it is kept: `sessionStorage`, under the signed-in person's principal, so a reload in the same
 * tab keeps it and another tab, browser or person does not see it. Not `localStorage`: the next person
 * on the same browser profile would find it. When storage is unavailable (a private window, blocked
 * site data) it is kept in memory for the page's life. Sign-out forgets it (`activity.forget()`), as the
 * tokens, which stay in memory, are forgotten: nothing of a person outlives the session in this tab.
 */
import * as v from 'valibot';
import { auth } from './auth.svelte.ts';

/** At most this many entries are kept, the newest. */
export const MAX_ENTRIES = 100;
/** No entry older than this is kept. */
export const MAX_AGE_MS = 24 * 60 * 60 * 1000;

const KEY_PREFIX = 'rask.activity:';
/** The one log of a stack that signs nobody in. */
const ANONYMOUS = '';
/** The memory-only log of a page whose sign-in is still completing; never written to storage. */
const NO_SESSION = '\0';

const OutcomeSchema = v.variant('ok', [
	v.object({ ok: v.literal(true) }),
	/** `status` is the HTTP status the door answered (0: it was unreachable), or `null` when the write
	 *  failed in the browser before any answer, a thrown error. */
	v.object({ ok: v.literal(false), status: v.nullable(v.number()), detail: v.string() }),
]);
export type ActivityOutcome = v.InferOutput<typeof OutcomeSchema>;

const EntrySchema = v.intersect([
	v.object({ id: v.string(), label: v.string(), at: v.number() }),
	OutcomeSchema,
]);
export type ActivityEntry = v.InferOutput<typeof EntrySchema>;

const StoredSchema = v.object({ entries: v.array(EntrySchema), lastOpened: v.number() });
type Stored = v.InferOutput<typeof StoredSchema>;

const EMPTY: Stored = { entries: [], lastOpened: 0 };

/** The entries worth keeping at `now`: none older than {@link MAX_AGE_MS}, at most
 *  {@link MAX_ENTRIES}, newest first (the order they are held in). */
export function prune(entries: readonly ActivityEntry[], now: number): ActivityEntry[] {
	return entries.filter((entry) => now - entry.at < MAX_AGE_MS).slice(0, MAX_ENTRIES);
}

/** The log as this tab holds it: the truth while the page lives. Storage only carries it across a
 *  reload, so a write storage refuses still lands here. A plain `Map` on purpose: storage is not
 *  reactive either, so the log's one reactive signal is the store's revision, moved by every write. */
const memory = new Map<string, Stored>();

function storage(): Storage | null {
	try {
		return window.sessionStorage;
	} catch {
		return null;
	}
}

/** This tab's log for a principal: memory when this page has held one, else what storage kept from
 *  before a reload (then held in memory from here on). */
function load(principal: string): Stored {
	const held = memory.get(principal);
	if (held) return held;
	let stored: Stored = EMPTY;
	try {
		const raw = storage()?.getItem(KEY_PREFIX + principal);
		if (raw) {
			const read = v.safeParse(StoredSchema, JSON.parse(raw));
			if (read.success) stored = read.output;
		}
	} catch {
		// Unreadable storage or a corrupt entry: nothing to rehydrate.
	}
	memory.set(principal, stored);
	return stored;
}

function save(principal: string, stored: Stored): void {
	memory.set(principal, stored);
	try {
		storage()?.setItem(KEY_PREFIX + principal, JSON.stringify(stored));
	} catch {
		// Storage refused (quota, blocked): memory keeps the log for this page's life.
	}
}

class ActivityLog {
	/** Moved by every write here, so what is derived from the log reads it again. */
	#revision = $state(0);

	/** Whose log this is: the signed-in person, the anonymous log of a stack that signs nobody in, or
	 *  nobody while sign-in is completing (a log held in memory only, keyed by no principal). */
	readonly #principal = $derived(auth.enabled ? (auth.session?.sub ?? null) : ANONYMOUS);

	readonly #stored = $derived.by((): Stored => {
		void this.#revision;
		const principal = this.#principal;
		return principal === null ? (memory.get(NO_SESSION) ?? EMPTY) : load(principal);
	});

	/** Newest first, pruned to what is kept. */
	get entries(): ActivityEntry[] {
		return prune(this.#stored.entries, Date.now());
	}

	/** The entries recorded since the panel was last opened. */
	get unread(): number {
		const lastOpened = this.#stored.lastOpened;
		return this.entries.filter((entry) => entry.at > lastOpened).length;
	}

	#write(next: Stored): void {
		const principal = this.#principal;
		if (principal === null) memory.set(NO_SESSION, next);
		else save(principal, next);
		this.#revision++;
	}

	/** Record one write's outcome. */
	record(label: string, outcome: ActivityOutcome): void {
		const now = Date.now();
		const entry: ActivityEntry = { id: crypto.randomUUID(), label, at: now, ...outcome };
		const stored = this.#stored;
		this.#write({ ...stored, entries: prune([entry, ...stored.entries], now) });
	}

	/** The panel was opened: everything recorded so far has been seen. */
	markRead(): void {
		const stored = this.#stored;
		this.#write({ ...stored, lastOpened: Date.now() });
	}

	/** Empty this person's log. */
	clear(): void {
		this.#write(EMPTY);
	}

	/** Sign-out: forget every log this tab holds, in storage and in memory. */
	forget(): void {
		memory.clear();
		const store = storage();
		try {
			if (store) {
				const keys = Array.from({ length: store.length }, (_, index) => store.key(index));
				for (const key of keys) if (key?.startsWith(KEY_PREFIX)) store.removeItem(key);
			}
		} catch {
			// Storage unreadable: nothing of the log was kept there.
		}
		this.#revision++;
	}
}

export const activity = new ActivityLog();
