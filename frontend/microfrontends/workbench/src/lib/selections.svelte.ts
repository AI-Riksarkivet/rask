/**
 * The compositor's relay log — what foreign panels' RASK_SELECT events become after the valibot
 * gate. A rune store handed to NATIVE panels via context (context is fine on the compositor's own
 * side of the boundary); foreign panels never see it — they get properties, not stores.
 */
import { createContext } from 'svelte';
import type { SelectDetail } from '@rask/dockview/contract';

export class Selections {
	rows = $state<SelectDetail[]>([]);
	/** Wave 3: the label being pushed DOWN into every list element as its `filtertext` property —
	 *  set by clicking a logged selection, cleared by the chip beside it. */
	filter = $state('');

	push(detail: SelectDetail): void {
		// Newest first, bounded — a relay log, not a database.
		this.rows = [detail, ...this.rows].slice(0, 50);
	}
}

export const [getSelections, setSelections] = createContext<Selections>();
