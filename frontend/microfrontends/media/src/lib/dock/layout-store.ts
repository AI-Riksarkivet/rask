/**
 * A {@link LayoutStore} for one media workbench, over the catalog's per-subject `dock-layout`
 * document on the Dapr state store.
 *
 * This zone needed NO new BFF route and NO new client: `routes/capi/v1/user-state/[document]` already
 * serves GET/PUT/DELETE, and `$lib/user-state` already implements the three-outcome contract. Adding
 * `'dock-layout'` to that module's document union was the whole of the wiring — which is the clearest
 * evidence available that the per-subject user-state path generalises, rather than having been shaped
 * around its first caller.
 *
 * Saving is a read-modify-write for the same reason as the lakehouse's: ONE document holds every
 * workbench this user has arranged, keyed by workbench id, so a blind PUT would delete the other
 * zones' layouts. Re-reading first costs a round trip that `LayoutAutosave`'s debounce already bounds.
 */
import type { LayoutRead, LayoutStore } from '@rask/dockview';
import type { SerializedDockview } from 'dockview';
import { readUserState, writeUserState } from '$lib/user-state';

/** Mirrors `service_kit.schemas.dock_layout.DockLayouts`. */
interface DockLayoutsDocument {
	workbenches: Record<string, SerializedDockview>;
}

export function makeLayoutStore(workbenchId: string): LayoutStore {
	return {
		async load(): Promise<LayoutRead> {
			const read = await readUserState<DockLayoutsDocument>('dock-layout');
			if (read.status === 'unreadable') return read;
			if (read.status === 'absent') return { status: 'absent' };
			// Guard SHAPE, not just JSON validity: a stored `null` / `42` parses fine but would make
			// `workbenches` a non-object and throw on the index below.
			const workbenches = read.value?.workbenches;
			if (typeof workbenches !== 'object' || workbenches === null) {
				return { status: 'unreadable', detail: 'the stored dock document has no workbenches map' };
			}
			const layout = workbenches[workbenchId];
			return layout === undefined ? { status: 'absent' } : { status: 'ok', layout };
		},

		async save(layout: SerializedDockview): Promise<boolean> {
			const read = await readUserState<DockLayoutsDocument>('dock-layout');
			// Refuse rather than clobber — the second lock behind LayoutAutosave's own refusal, for a
			// document that became unreadable after a healthy start.
			if (read.status === 'unreadable') return false;
			const existing = read.status === 'ok' ? (read.value?.workbenches ?? {}) : {};
			const next: DockLayoutsDocument = { workbenches: { ...existing, [workbenchId]: layout } };
			return writeUserState('dock-layout', next);
		},
	};
}
