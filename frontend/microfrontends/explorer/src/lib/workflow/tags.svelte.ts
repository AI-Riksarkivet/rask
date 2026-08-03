/**
 * The workflow's tag store — tags keyed by chunk identity (`hitKey`), so a chunk
 * can be tagged inline anywhere it appears (the results list) AND in bulk by a
 * Tagger node, with the tags persisting alongside the graph and flowing into
 * Export. A small focused runes class composed into the graph singleton.
 */
import type { Hit } from '@rask/media-api';
import { hitKey } from '$lib/utils';

/** Anything carrying the dataset's identity key fields — `hitKey` reads them
 *  through the active DatasetView, so a bare Row is enough. */
type ChunkRef = Hit;

export class WorkflowTags {
	/** tags keyed by `hitKey` (the dataset's identity key). */
	byChunk = $state<Record<string, string[]>>({});

	/** Pending un-tags awaiting the next tags-save — the hit is captured at removal
	 *  time so a chunk that later scrolls out of the results can still be un-tagged
	 *  server-side. Session-local (a pending edit, like the annotator's overlay):
	 *  deliberately NOT persisted with the graph. */
	removed = $state<Record<string, { hit: ChunkRef; labels: string[] }>>({});

	/** Current tags for a chunk (empty array when untagged). */
	forHit(hit: ChunkRef): string[] {
		return this.byChunk[hitKey(hit)] ?? [];
	}

	/** Toggle one tag on one chunk (inline tagging from a results row). A removal
	 *  is also queued for the next tags-save; re-adding cancels the queued removal. */
	toggle(hit: ChunkRef, tag: string): void {
		const t = tag.trim();
		if (!t) return;
		const k = hitKey(hit);
		const cur = this.byChunk[k] ?? [];
		const removing = cur.includes(t);
		const next = removing ? cur.filter((x) => x !== t) : [...cur, t];
		this.byChunk = { ...this.byChunk, [k]: next };
		if (removing) this._queueRemoval(k, hit, t);
		else this._cancelRemoval(k, [t]);
	}

	private _queueRemoval(k: string, hit: ChunkRef, tag: string): void {
		const cur = this.removed[k];
		const labels = cur && !cur.labels.includes(tag) ? [...cur.labels, tag] : (cur?.labels ?? [tag]);
		this.removed = { ...this.removed, [k]: { hit: cur?.hit ?? hit, labels } };
	}

	private _cancelRemoval(k: string, tags: readonly string[]): void {
		const cur = this.removed[k];
		if (!cur) return;
		const labels = cur.labels.filter((l) => !tags.includes(l));
		const next = { ...this.removed };
		if (labels.length) next[k] = { hit: cur.hit, labels };
		else delete next[k];
		this.removed = next;
	}

	/** Stamp `tags` (union, add-only) onto every chunk — the Tagger node's bulk
	 *  write and the inline add path. */
	addTo(hits: readonly ChunkRef[], tags: readonly string[]): void {
		const add = tags.map((t) => t.trim()).filter(Boolean);
		if (!add.length || !hits.length) return;
		const next = { ...this.byChunk };
		for (const h of hits) {
			const k = hitKey(h);
			const cur = next[k] ?? [];
			next[k] = [...cur, ...add.filter((t) => !cur.includes(t))];
			this._cancelRemoval(k, add);
		}
		this.byChunk = next;
	}

	/** The queued un-tags, for the writer. */
	removedEntries(): { hit: ChunkRef; labels: string[] }[] {
		return Object.values(this.removed);
	}

	/** Flush exactly the SENT entries from the removal queue (per hit+label, so an
	 *  un-tag queued while a save was in flight survives for the next save). */
	flushRemoved(sent: readonly { hit: ChunkRef; labels: readonly string[] }[]): void {
		const next = { ...this.removed };
		for (const e of sent) {
			const k = hitKey(e.hit);
			const cur = next[k];
			if (!cur) continue;
			const labels = cur.labels.filter((l) => !e.labels.includes(l));
			if (labels.length) next[k] = { hit: cur.hit, labels };
			else delete next[k];
		}
		this.removed = next;
	}

	/** Hits with their current tags attached from the store — used at export time
	 *  so a `tags` column reflects both inline and Tagger-node tags. */
	withTags(hits: readonly Hit[]): Hit[] {
		return hits.map((h) => ({ ...h, tags: this.forHit(h) }));
	}

	/** Replace the whole store (rehydrate from persistence, undo/redo restore).
	 *  Queued removals whose label the incoming state RESTORES are cancelled —
	 *  otherwise an undone un-tag would ride the next save and delete a tag the
	 *  UI shows as present. */
	hydrate(byChunk: Record<string, string[]>): void {
		this.byChunk = byChunk;
		const next: Record<string, { hit: ChunkRef; labels: string[] }> = {};
		for (const [k, entry] of Object.entries(this.removed)) {
			const restored = byChunk[k] ?? [];
			const labels = entry.labels.filter((l) => !restored.includes(l));
			if (labels.length) next[k] = { hit: entry.hit, labels };
		}
		this.removed = next;
	}

	/** A plain serialisable snapshot of the store (for persistence). */
	snapshot(): Record<string, string[]> {
		return { ...this.byChunk };
	}

	/** Clear all tags (full graph reset). */
	reset(): void {
		this.byChunk = {};
		this.removed = {};
	}
}
