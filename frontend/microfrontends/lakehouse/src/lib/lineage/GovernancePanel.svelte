<script lang="ts">
	// The dataset panel's governance section (#49): curated tag chips (add/remove) + an editable
	// description, with the last-writer attribution the backend stamps. Self-contained: fetches
	// /datasets/{name}/governance itself (latest-wins on dataset switches) and refetches after writes,
	// so the dataset detail page only mounts it with the route's dataset name.
	import { Plus, X } from '@lucide/svelte';
	import {
		addDatasetTag,
		fetchGovernance,
		removeDatasetTag,
		setDatasetDescription,
	} from './remote/lineage.remote';
	import type { DatasetGovernance } from '@rask/api/lineage';

	let { dataset }: { dataset: string } = $props();

	let governance = $state<DatasetGovernance | null>(null);
	let newTag = $state('');
	let editingDescription = $state(false);
	let draftDescription = $state('');
	let busy = $state(false);
	let error = $state<string | null>(null);
	let unavailable = $state(false);

	$effect(() => {
		const current = dataset;
		governance = null;
		error = null;
		unavailable = false;
		editingDescription = false;
		// `refresh()`, not a bare call: a query instance is cached by its argument, so re-mounting the
		// panel on the same dataset would render the record it already holds instead of re-reading.
		void (async () => {
			const record = fetchGovernance(current);
			await record.refresh();
			const g = record.current;
			// Latest-wins: ignore a response for a dataset the user has already clicked away from.
			if (dataset !== current) return;
			if (g) governance = g;
			else unavailable = true; // a swallowed fetch failure must not masquerade as "no metadata"
		})();
	});

	function fail(status: number, detail: string): void {
		if (status === 401) error = 'Sign in to edit governance metadata.';
		else if (status === 403) error = 'Denied: editing needs write access (can_write_data).';
		else error = detail;
	}

	async function submitTag(): Promise<void> {
		const tag = newTag.trim();
		if (!tag || busy) return;
		busy = true;
		error = null;
		try {
			const res = await addDatasetTag({ name: dataset, tag });
			if (res.ok) {
				governance = res.data;
				newTag = '';
			} else {
				fail(res.status, res.detail);
			}
		} finally {
			busy = false;
		}
	}

	async function dropTag(tag: string): Promise<void> {
		if (busy) return;
		busy = true;
		error = null;
		try {
			const res = await removeDatasetTag({ name: dataset, tag });
			if (res.ok) governance = res.data;
			else fail(res.status, res.detail);
		} finally {
			busy = false;
		}
	}

	async function saveDescription(): Promise<void> {
		if (busy) return;
		busy = true;
		error = null;
		try {
			const res = await setDatasetDescription({
				name: dataset,
				description: draftDescription.trim(),
			});
			if (res.ok) {
				governance = res.data;
				editingDescription = false;
			} else {
				fail(res.status, res.detail);
			}
		} finally {
			busy = false;
		}
	}

	function startEditing(): void {
		draftDescription = governance?.description ?? '';
		editingDescription = true;
	}
</script>

<div class="governance">
	{#if unavailable}
		<p class="mut">Governance metadata unavailable.</p>
	{:else if governance === null}
		<p class="mut">Loading governance…</p>
	{:else}
		{#if editingDescription}
			<div class="desc-edit">
				<textarea
					bind:value={draftDescription}
					rows="2"
					maxlength="2000"
					placeholder="What is this dataset?"></textarea>
				<div class="desc-actions">
					<button class="btn" disabled={busy} onclick={saveDescription}>Save</button>
					<button class="btn ghost" onclick={() => (editingDescription = false)}>Cancel</button>
				</div>
			</div>
		{:else}
			<button class="desc" onclick={startEditing} title="Edit description">
				{#if governance.description}
					<span>{governance.description}</span>
				{:else}
					<span class="placeholder">Add a description…</span>
				{/if}
			</button>
		{/if}

		<div class="tags">
			{#each governance.tags as tag (tag)}
				<span class="tag mono">
					{tag}
					<button class="tag-x" disabled={busy} title="Remove tag" onclick={() => dropTag(tag)}>
						<X size={10} />
					</button>
				</span>
			{/each}
			<form
				class="tag-add"
				onsubmit={(e) => {
					e.preventDefault();
					submitTag();
				}}
			>
				<input
					bind:value={newTag}
					maxlength="64"
					placeholder="add tag"
					aria-label="Add governance tag"
				/>
				<button class="btn ghost" type="submit" disabled={busy || !newTag.trim()}>
					<Plus size={12} />
				</button>
			</form>
		</div>

		{#if governance.tags_updated_by || governance.description_updated_by}
			<p class="attribution mono">
				{#if governance.tags_updated_by}
					tags curated: {governance.tags_updated_by} · {governance.tags_updated_at?.slice(0, 19)}
				{/if}
				{#if governance.description_updated_by}
					· description: {governance.description_updated_by} · {governance.description_updated_at?.slice(
						0,
						19,
					)}
				{/if}
			</p>
		{/if}

		{#if error}
			<p class="error">{error}</p>
		{/if}
	{/if}
</div>

<style>
	.governance {
		display: flex;
		flex-direction: column;
		gap: 6px;
		margin: 4px 0 10px;
	}
	.desc {
		text-align: left;
		background: none;
		border: none;
		padding: 0;
		color: var(--mut);
		font: inherit;
		font-size: 12px;
		cursor: pointer;
	}
	.desc .placeholder {
		color: var(--faint);
		font-style: italic;
	}
	.desc-edit textarea {
		width: 100%;
		background: var(--panel-2);
		border: 1px solid var(--line);
		border-radius: var(--radius-sm);
		color: var(--ink);
		font: inherit;
		font-size: 12px;
		padding: 6px 8px;
		resize: vertical;
	}
	.desc-actions {
		display: flex;
		gap: 6px;
		margin-top: 4px;
	}
	.btn {
		padding: 3px 10px;
		border: 1px solid var(--line);
		border-radius: 7px;
		background: var(--panel-2);
		color: var(--ink);
		font: inherit;
		font-size: 12px;
		cursor: pointer;
	}
	.btn:disabled {
		opacity: 0.5;
		cursor: default;
	}
	.btn.ghost {
		background: none;
	}
	.tags {
		display: flex;
		flex-wrap: wrap;
		align-items: center;
		gap: 5px;
	}
	.tag {
		display: inline-flex;
		align-items: center;
		gap: 3px;
		font-size: 10px;
		padding: 2px 4px 2px 8px;
		border: 1px solid var(--line);
		border-radius: 999px;
		color: var(--mut);
	}
	.tag-x {
		display: inline-flex;
		background: none;
		border: none;
		color: var(--faint);
		cursor: pointer;
		padding: 1px;
	}
	.tag-x:hover {
		color: var(--fail);
	}
	.tag-add {
		display: inline-flex;
		align-items: center;
		gap: 3px;
	}
	.tag-add input {
		width: 90px;
		background: var(--panel-2);
		border: 1px solid var(--line);
		border-radius: 999px;
		color: var(--ink);
		font-size: 10px;
		padding: 2px 8px;
	}
	.attribution {
		color: var(--faint);
		font-size: 10px;
		margin: 0;
	}
	.mut {
		color: var(--mut);
		font-size: 11px;
		margin: 0;
	}
	.error {
		color: var(--fail);
		font-size: 11px;
		margin: 0;
	}
</style>
