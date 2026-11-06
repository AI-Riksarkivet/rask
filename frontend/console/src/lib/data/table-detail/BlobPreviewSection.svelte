<script lang="ts">
	// #64 blob preview — one blob cell's bytes (GET /blobs?column=&row=), reader-tier can_read_data at
	// the catalog. Fetched with the person's bearer and shown from a blob URL: an `<img src>` cannot
	// carry a bearer. Only offered for binary/blob-typed columns, which this section derives from the
	// schema itself.
	//
	// Split out of TableDetail.svelte (#98). Mounted under the parent's `{#key table}`, so the
	// instance never survives a navigation.
	import { Select } from '@rask/ui/select';
	import { requestBinary } from '#lib/http.ts';
	import { CATALOG_API } from '#lib/upstreams.ts';
	import { typeName, type SchemaField } from './type-name';

	let { table, fields }: { table: string; fields: SchemaField[] } = $props();

	// Columns whose type is binary/blob — the ones the blob preview can read a cell from.
	const blobColumns = $derived(
		fields
			.filter((f) => /binary|blob/i.test(typeName(f.type)))
			.map((f) => f.name)
			.filter((n): n is string => !!n),
	);

	let blobCol = $state('');
	let blobRow = $state<number | null>(null);
	let blobSrc = $state<string | null>(null);
	let blobFailed = $state(false);
	let blobError = $state<string | null>(null);

	async function previewBlob(): Promise<void> {
		if (!blobCol || blobRow == null) return;
		blobFailed = false;
		blobError = null;
		const res = await requestBinary(
			CATALOG_API,
			`management/v1/table/${encodeURIComponent(table)}/blobs?column=${encodeURIComponent(blobCol)}&row=${blobRow}`,
		);
		if (blobSrc) URL.revokeObjectURL(blobSrc);
		blobSrc = null;
		if (!res.ok) {
			blobError = `${res.status || 'offline'}: ${res.detail}`;
			return;
		}
		blobSrc = URL.createObjectURL(new Blob([res.data]));
	}

	$effect(() => () => {
		if (blobSrc) URL.revokeObjectURL(blobSrc);
	});
</script>

<section>
	<h2>Blob preview</h2>
	{#if blobColumns.length === 0}
		<p class="mut">No blob columns on this table.</p>
	{:else}
		<div class="refs tagform">
			<Select
				bind:value={blobCol}
				ariaLabel="Blob column"
				placeholder="column…"
				options={blobColumns.map((c) => ({ value: c, label: c }))}
			/>
			<input class="mono" type="number" min="0" placeholder="row" bind:value={blobRow} />
			<button class="btn" disabled={!blobCol || blobRow == null} onclick={previewBlob}
				>Preview</button
			>
		</div>
		{#if blobError}
			<p class="mut">Blob unavailable — {blobError}</p>
		{/if}
		{#if blobSrc}
			{#if blobFailed}
				<p class="mut">
					<!-- eslint-disable-next-line svelte/no-navigation-without-resolve -- a blob: URL, not a route -->
					Not an inline-previewable image — <a href={blobSrc} download>save the blob</a>.
				</p>
			{:else}
				<img class="blob" src={blobSrc} alt="blob preview" onerror={() => (blobFailed = true)} />
			{/if}
		{/if}
	{/if}
</section>

<style>
	section {
		margin-bottom: 22px;
	}
	h2 {
		font-size: 13px;
		text-transform: uppercase;
		letter-spacing: 0.06em;
		color: var(--faint);
		margin: 0 0 8px;
	}
	.mut {
		color: var(--faint);
		font-size: 12px;
	}
	.refs {
		display: flex;
		flex-wrap: wrap;
		align-items: center;
		gap: 6px;
		margin-bottom: 6px;
		font-size: 12px;
	}
	.tagform input {
		width: 150px;
	}
	.blob {
		display: block;
		max-width: 100%;
		max-height: 320px;
		border: 1px solid var(--line);
		border-radius: var(--radius-sm);
		margin-top: 10px;
	}
	.btn {
		background: var(--panel-2);
		border: 1px solid var(--line);
		border-radius: var(--radius-sm);
		color: var(--ink);
		font-size: 12px;
		padding: 3px 10px;
		cursor: pointer;
	}
	.btn:disabled {
		opacity: 0.5;
		cursor: default;
	}
</style>
