/**
 * Framework-agnostic layer/grouping store — group-by column, per-group visibility
 * and colors. NO Svelte runes; the Svelte binding (src/lib/stores/layers.svelte.ts)
 * subscribes via `on`/`emit` and mirrors state into runes.
 */
export class LayerStore {
	groupByColumn = 'label';
	hiddenGroups = new Set<string>();
	groupColors = new Map<string, number>();

	private _listeners = new Set<() => void>();

	/** Subscribe to any change. Returns an unsubscribe fn. */
	on(listener: () => void): () => void {
		this._listeners.add(listener);
		return () => this._listeners.delete(listener);
	}

	private emit(): void {
		for (const listener of this._listeners) listener();
	}

	toggleVisibility(group: string): void {
		const next = new Set(this.hiddenGroups);
		if (next.has(group)) next.delete(group);
		else next.add(group);
		this.hiddenGroups = next;
		this.emit();
	}

	setColor(group: string, hex: number): void {
		const next = new Map(this.groupColors);
		next.set(group, hex);
		this.groupColors = next;
		this.emit();
	}

	setGroupBy(column: string): void {
		this.groupByColumn = column;
		this.hiddenGroups = new Set();
		this.groupColors = new Map();
		this.emit();
	}

	isHidden(group: string): boolean {
		return this.hiddenGroups.has(group);
	}

	getColor(group: string): number | undefined {
		return this.groupColors.get(group);
	}
}
