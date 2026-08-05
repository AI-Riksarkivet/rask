<script lang="ts">
	// #74 tail / #78 — table + per-column property editor. BOTH levels speak the same dialect: the write
	// MERGES by key and a `null` value DELETES one (schema_metadata/update at the table level,
	// update_field_metadata per column). So a removed row must be SENT as null — omitting it leaves the key
	// on the table, which is what the × button used to do while reporting "Saved." Both are writer-gated at
	// the catalog and reach it only through the session-only /capi columns BFF (the signed-in user's bearer).
	//
	// `description` is RESERVED: it is the table's own prose, rendered on the detail header, so it gets a
	// dedicated multi-line field here and is filtered out of the raw key/value grid. One editor, one door.
	import { Select } from '@rask/ui/select';
	import { untrack } from 'svelte';
	import type { CatalogResult } from './catalog';
	import { setFieldMetadata, setTableProperties } from './remote/catalog.remote';

	type Field = { name: string; metadata?: Record<string, string> };
	let {
		table,
		fields,
		tableMeta,
		onchange,
	}: {
		table: string;
		fields: Field[];
		tableMeta: Record<string, string>;
		onchange: () => void;
	} = $props();

	const DESCRIPTION = 'description';

	let rows = $state<{ key: string; value: string }[]>([]);
	let description = $state('');
	/** The keys the editor was seeded with — a seeded key that is gone at save time is a DELETE, and the
	 *  only way to say so on the wire is an explicit null. Without this snapshot removal is unexpressible. */
	let seeded = $state<{ keys: string[]; description: string }>({ keys: [], description: '' });
	let busy = $state(false);
	let error = $state<string | null>(null);
	let savedMsg = $state<string | null>(null);

	// Per-column properties.
	let selectedCol = $state('');
	let colKey = $state('');
	let colValue = $state('');

	// Seed the editable table-props rows from the current metadata, keyed ONLY on the table id (tableMeta is
	// read untracked). A post-save reload changes tableMeta but not the table, so it must NOT re-run — that
	// would wipe the "Saved." confirmation and any in-progress edits. Navigating to another table remounts
	// this component (the parent blanks detail → Loading), so the fresh mount reseeds from the new metadata.
	$effect(() => {
		void table;
		untrack(() => {
			rows = Object.entries(tableMeta)
				.filter(([key]) => key !== DESCRIPTION)
				.map(([key, value]) => ({ key, value }));
			description = tableMeta[DESCRIPTION] ?? '';
			seeded = { keys: Object.keys(tableMeta), description };
			error = null;
			savedMsg = null;
			selectedCol = '';
			colKey = '';
			colValue = '';
		});
	});

	const selectedField = $derived(fields.find((f) => f.name === selectedCol) ?? null);
	const colEntries = $derived(Object.entries(selectedField?.metadata ?? {}));

	function fail(status: number, detail: string): void {
		if (status === 401) error = 'Sign in to edit properties.';
		else if (status === 403)
			error = 'Denied: editing properties needs writer access (can_write_data).';
		else error = detail;
	}

	async function run(fn: () => Promise<CatalogResult<unknown>>, ok?: () => void): Promise<void> {
		if (busy) return;
		busy = true;
		error = null;
		savedMsg = null;
		try {
			const res = await fn();
			if (res.ok) {
				ok?.();
				onchange();
			} else fail(res.status, res.detail);
		} finally {
			busy = false;
		}
	}

	function saveTableProps(): void {
		// The write MERGES, so this is the set of CHANGES: every live row upserts, every seeded key that is
		// no longer here is sent as null to delete it. Blank keys dropped; the last occurrence of a
		// duplicated key wins. The description rides the SAME post — a second call would be a second Lance
		// version for one edit — and is applied last, so the dedicated field wins over a hand-typed row.
		const map: Record<string, string | null> = {};
		for (const r of rows) {
			const k = r.key.trim();
			if (k && k !== DESCRIPTION) map[k] = r.value;
		}
		for (const k of seeded.keys) {
			if (k !== DESCRIPTION && !(k in map)) map[k] = null;
		}
		const desc = description.trim();
		if (desc) map[DESCRIPTION] = description;
		else if (seeded.description) map[DESCRIPTION] = null;
		run(
			() => setTableProperties({ table, metadata: map }),
			() => (savedMsg = 'Saved.'),
		);
	}

	function setColKey(): void {
		const path = selectedCol;
		const key = colKey.trim();
		if (!path || !key) return;
		run(
			() => setFieldMetadata({ table, path, metadata: { [key]: colValue } }),
			() => {
				colKey = '';
				colValue = '';
			},
		);
	}

	function deleteColKey(key: string): void {
		if (!selectedCol) return;
		run(() => setFieldMetadata({ table, path: selectedCol, metadata: { [key]: null } }));
	}
</script>

<section>
	<h2>Properties</h2>

	<h3>Description</h3>
	<p class="mut">
		What this table is. Stored as the reserved <code class="mono">description</code> property and shown
		under the table's name.
	</p>
	<textarea
		bind:value={description}
		rows="3"
		placeholder="What this table holds, where it comes from, what to watch out for…"
		aria-label="Table description"></textarea>

	<h3>Table properties</h3>
	<p class="mut">
		Schema-level key/value metadata. Saving merges these keys; removing a row deletes it.
	</p>
	<div class="rows">
		{#each rows as row, i (i)}
			<div class="kv">
				<input
					class="mono"
					bind:value={row.key}
					placeholder="key"
					aria-label="Property key {i + 1}"
				/>
				<input
					class="mono"
					bind:value={row.value}
					placeholder="value"
					aria-label="Property value {i + 1}"
				/>
				<button
					class="chip-x"
					title="remove"
					aria-label="Remove property {i + 1}"
					onclick={() => (rows = rows.filter((_, j) => j !== i))}>×</button
				>
			</div>
		{/each}
	</div>
	<div class="row">
		<button class="btn ghost" onclick={() => (rows = [...rows, { key: '', value: '' }])}
			>+ add row</button
		>
		<button class="btn" disabled={busy} onclick={saveTableProps}>Save properties</button>
		{#if savedMsg}<span class="okmsg">{savedMsg}</span>{/if}
	</div>

	<h3>Column properties</h3>
	<p class="mut">Per-field metadata, merged by key (delete removes just that key).</p>
	<div class="row">
		<Select
			bind:value={selectedCol}
			ariaLabel="Column for properties"
			placeholder="pick a column"
			options={fields.map((f) => ({ value: f.name, label: f.name }))}
		/>
	</div>
	{#if selectedCol}
		{#if colEntries.length > 0}
			<div class="chips">
				{#each colEntries as [k, v] (k)}
					<span class="chip mono"
						>{k}={v}
						<button
							class="chip-x"
							title="delete {k}"
							aria-label="Delete column property {k}"
							disabled={busy}
							onclick={() => deleteColKey(k)}>×</button
						></span
					>
				{/each}
			</div>
		{:else}
			<p class="mut">No metadata on this column yet.</p>
		{/if}
		<div class="kv">
			<input class="mono" bind:value={colKey} placeholder="key" aria-label="Column property key" />
			<input
				class="mono"
				bind:value={colValue}
				placeholder="value"
				aria-label="Column property value"
			/>
			<button class="btn" disabled={busy || !colKey.trim()} onclick={setColKey}>Set</button>
		</div>
	{/if}

	{#if error}<p class="error">{error}</p>{/if}
</section>

<style>
	section {
		margin-top: 26px;
	}
	h2 {
		font-size: 15px;
		margin: 0 0 8px;
	}
	h3 {
		font-size: 12px;
		color: var(--faint);
		font-weight: 600;
		margin: 16px 0 4px;
		text-transform: uppercase;
		letter-spacing: 0.04em;
	}
	.mut {
		color: var(--mut);
		font-size: 12px;
		margin: 0 0 8px;
	}
	.rows {
		display: flex;
		flex-direction: column;
		gap: 6px;
	}
	.kv {
		display: flex;
		gap: 6px;
		align-items: center;
		margin-top: 6px;
	}
	.row {
		display: flex;
		gap: 8px;
		align-items: center;
		margin-top: 8px;
		flex-wrap: wrap;
	}
	input {
		background: var(--panel-2);
		border: 1px solid var(--line);
		border-radius: var(--radius-sm);
		color: var(--ink);
		font-size: 12px;
		padding: 4px 8px;
		flex: 1 1 140px;
		min-width: 100px;
	}
	/* Deliberately NOT @rask/ui's Textarea: this section is on the legacy --ink/--line bridge at 12px, and
	   the Tailwind component would land a 16px rounded-lg field among 12px radius-sm ones. */
	textarea {
		background: var(--panel-2);
		border: 1px solid var(--line);
		border-radius: var(--radius-sm);
		color: var(--ink);
		font: inherit;
		font-size: 12px;
		line-height: 1.5;
		padding: 6px 8px;
		width: 100%;
		resize: vertical;
	}
	.btn {
		background: var(--panel-2);
		border: 1px solid var(--line);
		border-radius: var(--radius-sm);
		color: var(--ink);
		font-size: 12px;
		padding: 4px 12px;
		cursor: pointer;
	}
	.btn.ghost {
		background: transparent;
	}
	.btn:disabled {
		opacity: 0.5;
		cursor: default;
	}
	.chips {
		display: flex;
		flex-wrap: wrap;
		gap: 6px;
		margin-top: 6px;
	}
	.chip {
		display: inline-flex;
		align-items: center;
		gap: 4px;
		background: var(--panel-2);
		border: 1px solid var(--line);
		border-radius: var(--radius-sm);
		padding: 1px 4px 1px 8px;
		font-size: 12px;
	}
	.chip-x {
		background: transparent;
		border: none;
		color: var(--faint);
		cursor: pointer;
		font-size: 13px;
		padding: 0 4px;
	}
	.chip-x:hover {
		color: var(--fail);
	}
	.okmsg {
		color: var(--ok);
		font-size: 12px;
	}
	.error {
		color: var(--fail);
		font-size: 12px;
		margin-top: 8px;
	}
</style>
