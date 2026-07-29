/**
 * Undo/redo as a small focused runes class — snapshot strings on two stacks.
 * The graph owns snapshot()/restore() and decides WHEN to checkpoint (debounced
 * after changes settle); this just owns the stacks. Keeps WorkflowGraph lean.
 */
export class UndoHistory {
	private undoStack = $state<string[]>([]);
	private redoStack = $state<string[]>([]);
	private limit: number;

	constructor(limit = 50) {
		this.limit = limit;
	}

	get canUndo(): boolean {
		return this.undoStack.length > 0;
	}
	get canRedo(): boolean {
		return this.redoStack.length > 0;
	}

	/** Record `snapshot` (the pre-change state) and invalidate redo. */
	push(snapshot: string): void {
		this.undoStack = [...this.undoStack, snapshot].slice(-this.limit);
		this.redoStack = [];
	}

	/** Pop the undo stack; stashes `current` for redo. Returns the snapshot to
	 *  restore, or null when there is nothing to undo. */
	undo(current: string): string | null {
		const prev = this.undoStack.at(-1);
		if (prev === undefined) return null;
		this.redoStack = [...this.redoStack, current].slice(-this.limit);
		this.undoStack = this.undoStack.slice(0, -1);
		return prev;
	}

	/** Pop the redo stack; stashes `current` for undo. Returns the snapshot to
	 *  restore, or null when there is nothing to redo. */
	redo(current: string): string | null {
		const next = this.redoStack.at(-1);
		if (next === undefined) return null;
		this.undoStack = [...this.undoStack, current].slice(-this.limit);
		this.redoStack = this.redoStack.slice(0, -1);
		return next;
	}

	/** Drop all history (full graph reset). */
	clear(): void {
		this.undoStack = [];
		this.redoStack = [];
	}
}
