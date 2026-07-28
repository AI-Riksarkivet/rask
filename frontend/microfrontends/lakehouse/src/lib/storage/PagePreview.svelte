<script lang="ts">
	import { FileWarning, Images } from '@lucide/svelte';
	import { fmtSize, listPages, pageImageUrl, type Page } from './storage';

	// The document viewer, as a PANEL of the object browser rather than a separate destination.
	//
	// A Lance dataset is a directory of objects, so walking into one with the plain browser shows
	// `.lance` fragments and a `_versions` manifest — the storage layout, not the document. This
	// reads the same location as what it actually is: rows carrying a blob-v2 image column.
	//
	// The bytes never travel through this component. `pageImageUrl` hands the browser a URL and the
	// <img> streams it, so a 900 KB page costs one image request rather than a base64 round-trip
	// through JSON.
	let { dataset }: { dataset: string } = $props();

	let pages = $state<Page[]>([]);
	let selected = $state<number | null>(null);
	let status = $state(0);
	let detail = $state('');
	let settled = $state(false);

	async function load(): Promise<void> {
		settled = false;
		const want = dataset;
		const res = await listPages(want);
		if (dataset !== want) return; // latest-wins: the user moved on while this was in flight
		settled = true;
		if (res.ok) {
			pages = res.data.pages;
			// Land on the first page that actually has bytes — selecting a failed harvest by default
			// would open the viewer on a broken image.
			selected = pages.find((p) => p.has_image)?.id ?? null;
			status = 200;
			detail = '';
		} else {
			pages = [];
			selected = null;
			status = res.status;
			detail = res.detail;
		}
	}

	$effect(() => {
		void dataset;
		void load();
	});

	const current = $derived(pages.find((p) => p.id === selected) ?? null);
</script>

<section class="pages">
	<header>
		<Images size={14} />
		<h3>Pages</h3>
		{#if settled && status === 200}<span class="count">{pages.length}</span>{/if}
	</header>

	{#if !settled}
		<p class="muted">Reading the dataset…</p>
	{:else if status !== 200}
		<p class="err">Could not read pages (HTTP {status}){detail ? ` — ${detail}` : ''}.</p>
	{:else if pages.length === 0}
		<p class="muted">This dataset has no page rows.</p>
	{:else}
		<ol class="strip">
			{#each pages as page (page.id)}
				<li>
					<button
						class="thumb"
						class:active={page.id === selected}
						disabled={!page.has_image}
						onclick={() => (selected = page.id)}
						title={page.has_image ? `Page ${page.id}` : `Page ${page.id} — no image payload`}
					>
						{#if page.has_image}
							<img src={pageImageUrl(dataset, page.id)} alt="Page {page.id}" loading="lazy" />
						{:else}
							<span class="missing"><FileWarning size={14} /></span>
						{/if}
						<span class="n">{page.id}</span>
					</button>
				</li>
			{/each}
		</ol>

		{#if current}
			<figure>
				{#if current.has_image}
					<img class="full" src={pageImageUrl(dataset, current.id)} alt="Page {current.id}" />
				{:else}
					<p class="err">This page has no image payload — the harvest produced none.</p>
				{/if}
				<figcaption>
					<span><b>id</b> {current.id}</span>
					<span><b>stage</b> {current.stage}</span>
					<span><b>size</b> {fmtSize(current.size)}</span>
					{#if current.source_uri}<span class="uri" title={current.source_uri}>
							<b>source</b> {current.source_uri}
						</span>{/if}
				</figcaption>
			</figure>
		{/if}
	{/if}
</section>

<style>
	.pages {
		display: flex;
		flex-direction: column;
		gap: 0.6rem;
		min-height: 0;
	}
	header {
		display: flex;
		align-items: center;
		gap: 0.4rem;
	}
	h3 {
		font-size: 0.8rem;
		font-weight: 600;
	}
	.count {
		font-size: 0.7rem;
		color: var(--color-muted-foreground);
	}
	.strip {
		display: flex;
		gap: 0.4rem;
		overflow-x: auto;
		padding-bottom: 0.25rem;
	}
	.thumb {
		position: relative;
		width: 3.25rem;
		height: 4.25rem;
		border: 1px solid var(--color-border);
		border-radius: var(--radius-sm);
		overflow: hidden;
		background: var(--color-muted);
		cursor: pointer;
	}
	.thumb.active {
		outline: 2px solid var(--color-primary);
		outline-offset: 1px;
	}
	.thumb:disabled {
		cursor: not-allowed;
		opacity: 0.55;
	}
	.thumb img {
		width: 100%;
		height: 100%;
		object-fit: cover;
	}
	.missing {
		display: grid;
		place-items: center;
		height: 100%;
		color: var(--color-muted-foreground);
	}
	.n {
		position: absolute;
		right: 2px;
		bottom: 1px;
		font-size: 0.6rem;
		padding: 0 0.2rem;
		border-radius: 2px;
		background: color-mix(in oklab, var(--color-background) 75%, transparent);
	}
	figure {
		display: grid;
		gap: 0.4rem;
		min-height: 0;
	}
	.full {
		max-width: 100%;
		max-height: 26rem;
		object-fit: contain;
		border: 1px solid var(--color-border);
		border-radius: var(--radius-sm);
		background: var(--color-muted);
	}
	figcaption {
		display: flex;
		flex-wrap: wrap;
		gap: 0.6rem;
		font-size: 0.7rem;
		color: var(--color-muted-foreground);
	}
	.uri {
		max-width: 100%;
		overflow: hidden;
		text-overflow: ellipsis;
		white-space: nowrap;
	}
	.muted {
		color: var(--color-muted-foreground);
		font-size: 0.78rem;
	}
	.err {
		color: var(--color-destructive);
		font-size: 0.78rem;
	}
</style>
