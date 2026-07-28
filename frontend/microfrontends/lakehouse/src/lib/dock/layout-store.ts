/**
 * A {@link LayoutStore} for one workbench, backed by the catalog's per-subject `dock-layout` document
 * on the Dapr state store.
 *
 * One document holds EVERY workbench this user has arranged, keyed by workbench id — which is why
 * `UserStateDocument` did not have to grow a member per workbench, and why saving is a
 * **read-modify-write** rather than a blind PUT. A blind PUT of `{ workbenches: { lineage: … } }` would
 * delete every other workbench in the document, so a user with a lineage dock open in one tab and a
 * tables dock in another would lose whichever they touched second. Re-reading first costs one extra
 * round trip per save, and saves are already debounced to at most one per second by `LayoutAutosave`.
 *
 * (That still leaves last-write-wins WITHIN a single workbench opened twice, which is what the store's
 * `put` docstring already accepts: "this is one person's own work, and the localStorage it replaces had
 * exactly those semantics across their own tabs.")
 */
import type { LayoutRead, LayoutStore } from '@rask/dockview';
import type { SerializedDockview } from 'dockview';
import { readUserState, writeUserState } from '$lib/dock/user-state';

/** The shape of the `dock-layout` document — mirrors `service_kit.schemas.dock_layout.DockLayouts`. */
interface DockLayoutsDocument {
	workbenches: Record<string, SerializedDockview>;
}

export function makeLayoutStore(workbenchId: string): LayoutStore {
	return {
		async load(): Promise<LayoutRead> {
			const read = await readUserState<DockLayoutsDocument>('dock-layout');
			if (read.status === 'unreadable') return read;
			if (read.status === 'absent') return { status: 'absent' };
			// Guard SHAPE, not just JSON validity: a stored `null` / `42` / `{}` parses fine but would
			// make `workbenches` a non-object, and the indexing below would throw on first paint.
			const workbenches = read.value?.workbenches;
			if (typeof workbenches !== 'object' || workbenches === null) {
				return { status: 'unreadable', detail: 'the stored dock document has no workbenches map' };
			}
			const layout = workbenches[workbenchId];
			// This workbench absent from an otherwise-fine document is genuinely absent — the user has
			// simply never arranged THIS one. Saving is safe.
			return layout === undefined ? { status: 'absent' } : { status: 'ok', layout };
		},

		async save(layout: SerializedDockview): Promise<boolean> {
			const read = await readUserState<DockLayoutsDocument>('dock-layout');
			// Refuse rather than clobber: an unreadable document is exactly the case where a write
			// destroys work. `LayoutAutosave` also refuses after an unreadable LOAD, so this is the
			// second lock — it catches a document that became unreadable after a healthy start.
			if (read.status === 'unreadable') return false;
			const existing = read.status === 'ok' ? (read.value?.workbenches ?? {}) : {};
			const next: DockLayoutsDocument = { workbenches: { ...existing, [workbenchId]: layout } };
			return writeUserState('dock-layout', next);
		},
	};
}
