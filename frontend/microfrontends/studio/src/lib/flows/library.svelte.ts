/**
 * Saved flows — the flow LIBRARY, so a second experiment does not destroy the first.
 *
 * Until now the builder held exactly one graph: the autosave key. That is the gap that made this a
 * demo rather than a tool, and it is Langflow's whole organising idea (a list of flows you open).
 *
 * SCOPE, stated rather than implied: this is localStorage, keyed per flow NAME, and it is a
 * scaffold of the real thing. The real thing is the catalog's per-subject user-state document —
 * the explorer zone already declares a `'workflow-graph'` document for exactly this and never
 * migrated to it either. When that lands, `list`/`load`/`save`/`remove` are the four calls to
 * re-point, and the `unreadable` outcome must refuse to overwrite (the dock-layout protocol);
 * nothing else here changes shape.
 *
 * Snapshots are opaque strings on purpose: the store owns `snapshot()`/`applyParsed()`, so the
 * library never learns the graph's schema and cannot drift from it.
 */
import { browser } from '$app/environment';

/** One saved flow: a name and the snapshot string the graph store produced. */
export interface SavedFlow {
	name: string;
	/** The `graph.snapshot()` string. Parsed by the store's own valibot boundary on load. */
	snapshot: string;
	/** ISO timestamp of the last save — display only, never used for ordering decisions. */
	savedAt: string;
}

const LIBRARY_KEY = 'studio-flow-library-v1';

function readAll(): SavedFlow[] {
	if (!browser) return [];
	try {
		const raw = localStorage.getItem(LIBRARY_KEY);
		if (!raw) return [];
		const parsed: unknown = JSON.parse(raw);
		if (!Array.isArray(parsed)) return [];
		// Parse, don't validate: a malformed entry is dropped rather than failing the whole list,
		// so one bad save cannot cost a user every other flow they saved.
		return parsed.flatMap((e) => {
			if (typeof e !== 'object' || e === null) return [];
			const { name, snapshot, savedAt } = e as Record<string, unknown>;
			if (typeof name !== 'string' || typeof snapshot !== 'string') return [];
			return [{ name, snapshot, savedAt: typeof savedAt === 'string' ? savedAt : '' }];
		});
	} catch {
		return [];
	}
}

function writeAll(flows: SavedFlow[]): void {
	if (!browser) return;
	try {
		localStorage.setItem(LIBRARY_KEY, JSON.stringify(flows));
	} catch {
		// Storage full or disabled. Best-effort, exactly like the autosave — but the caller is told,
		// because losing a deliberate Save is not the same as losing an autosave tick.
		throw new Error('Could not save — browser storage is full or disabled.');
	}
}

/** The library as reactive state: a runes class so the toolbar's picker updates on every save. */
class FlowLibrary {
	flows = $state.raw<SavedFlow[]>(browser ? readAll() : []);
	/** The name last saved or loaded — what the picker shows as current. */
	current = $state<string>('');
	/** Last failure to surface (storage full, empty name). */
	error = $state<string | null>(null);

	get names(): string[] {
		return this.flows.map((f) => f.name);
	}

	/** Save `snapshot` under `name`, replacing any flow of that name. */
	save(name: string, snapshot: string): boolean {
		const trimmed = name.trim();
		if (!trimmed) {
			this.error = 'Give the flow a name first.';
			return false;
		}
		const savedAt = new Date().toISOString();
		const next = [
			...this.flows.filter((f) => f.name !== trimmed),
			{ name: trimmed, snapshot, savedAt },
		].sort((a, b) => (a.name < b.name ? -1 : 1));
		try {
			writeAll(next);
		} catch (err) {
			this.error = err instanceof Error ? err.message : String(err);
			return false;
		}
		this.flows = next;
		this.current = trimmed;
		this.error = null;
		return true;
	}

	/** The snapshot saved under `name`, or null. */
	load(name: string): string | null {
		const found = this.flows.find((f) => f.name === name);
		if (!found) return null;
		this.current = name;
		this.error = null;
		return found.snapshot;
	}

	/** Forget a saved flow. */
	remove(name: string): void {
		const next = this.flows.filter((f) => f.name !== name);
		try {
			writeAll(next);
		} catch (err) {
			this.error = err instanceof Error ? err.message : String(err);
			return;
		}
		this.flows = next;
		if (this.current === name) this.current = '';
	}
}

/** The singleton, imported by the toolbar. */
export const library = new FlowLibrary();
