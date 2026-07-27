<script lang="ts">
	// The landing/selection view — replaces the old hardcoded demo unit. Datasets come
	// from the viewer BFF (/api/datasets); picking one loads its descriptor (the same
	// schema-agnostic DatasetView the media zone renders from), then a paged documents
	// grid with thumbnails, then a per-document chunk picker that opens the annotate
	// canvas via `?keys=` (the existing deep-link contract — the read plane's bridge).
	import { onMount } from 'svelte';
	import { ChevronLeft, ChevronRight } from '@lucide/svelte';
	import {
		getDatasetView,
		getHealth,
		listDatasets,
		listDocuments,
		type Document,
		type DocumentsResponse,
	} from '@rask/media-api';
	import { setActiveView, type DatasetView } from '@rask/media-api/descriptor';
	import { Button } from '@rask/ui/button';
	import { Select } from '@rask/ui/select';
	import DocTile from './DocTile.svelte';
	import ChunkPicker from './ChunkPicker.svelte';

	let {
		onopen,
		initialDataset = null,
	}: {
		/** Open these key-paths in the canvas; `dataset` = the picked dataset's selector
		 *  (null for the backend default) so the deep link carries `?dataset=`. */
		onopen: (keys: string[], dataset: string | null) => void;
		/** Dataset to restore on mount (the URL's `?dataset=` — e.g. exiting the canvas). */
		initialDataset?: string | null;
	} = $props();

	interface DatasetEntry {
		id: string;
		docs: number | null;
	}

	const PER_PAGE = 24;

	let datasets = $state<DatasetEntry[]>([]);
	let view = $state<DatasetView | null>(null);
	let error = $state<string | null>(null);
	let docsPage = $state<DocumentsResponse | null>(null);
	let loadingDocs = $state(false);
	let openDoc = $state<Document | null>(null);

	const totalPages = $derived(docsPage ? Math.max(1, Math.ceil(docsPage.total / PER_PAGE)) : 1);

	// Latest-wins token: every dataset pick bumps it, and any response (view or docs)
	// from a superseded pick is dropped — a slow dataset A must neither revert a newer
	// pick of B nor mix A's docs under B's view (listDocuments reads the module-level
	// activeView() singleton at call time).
	let pickSeq = 0;

	async function loadDocs(page: number): Promise<void> {
		const seq = pickSeq;
		loadingDocs = true;
		try {
			const next = await listDocuments(page, PER_PAGE);
			if (seq !== pickSeq) return; // stale — a newer dataset pick owns the state now
			docsPage = next;
			error = null; // a successful load recovers from any earlier failure
		} catch (e) {
			if (seq !== pickSeq) return;
			error = e instanceof Error ? e.message : String(e);
		} finally {
			if (seq === pickSeq) loadingDocs = false;
		}
	}

	async function selectDataset(id: string, defaultId: string | null): Promise<void> {
		const seq = ++pickSeq;
		error = null;
		openDoc = null;
		docsPage = null;
		try {
			const next = await getDatasetView(id, id === defaultId);
			if (seq !== pickSeq) return; // superseded by a newer pick — don't revert it
			setActiveView(next); // the module-level singleton the URL helpers read
			view = next;
			datasetChoice = id;
			await loadDocs(1);
		} catch (e) {
			if (seq !== pickSeq) return;
			error = e instanceof Error ? e.message : String(e);
		}
	}

	// Recovery from a failed load — re-run whatever failed (initial listing, dataset
	// pick, or a docs page) instead of dead-ending the landing on one transient blip.
	async function retry(): Promise<void> {
		if (!view) return init();
		if (datasetChoice && datasetChoice !== view.id) return selectDataset(datasetChoice, defaultId);
		return loadDocs(docsPage?.page ?? 1);
	}

	let defaultId = $state<string | null>(null);

	// The dataset picker binds here (bits-ui Select is bind-only); a change loads
	// the picked dataset. Guarded so programmatic sync never re-loads the same one.
	let datasetChoice = $state('');
	$effect(() => {
		if (datasetChoice && datasetChoice !== view?.id) {
			void selectDataset(datasetChoice, defaultId);
		}
	});

	async function init(): Promise<void> {
		error = null;
		try {
			// The default dataset id derives from the health endpoint's db path (the same
			// convention as the media zone's descriptor store) so its URLs skip `?dataset=`.
			const [ds, health] = await Promise.all([listDatasets(), getHealth().catch(() => null)]);
			datasets = ds.map((d) => ({
				id: d.id,
				docs: d.tables?.['documents']?.row_count ?? null,
			}));
			defaultId =
				health?.db.path
					.replace(/\.lance$/, '')
					.split('/')
					.pop() ?? null;
			const first =
				ds.find((d) => d.id === initialDataset) ?? ds.find((d) => d.id === defaultId) ?? ds[0];
			if (first) await selectDataset(first.id, defaultId);
			else error = 'No datasets available from the viewer service.';
		} catch (e) {
			error = e instanceof Error ? e.message : String(e);
		}
	}

	onMount(() => void init());
</script>

<div class="flex h-full min-h-0 flex-col gap-3 overflow-hidden p-4" data-testid="data-selection">
	{#if openDoc && view}
		<!-- The handoff carries the picked dataset: default ⇒ null (no ?dataset= param). -->
		<ChunkPicker
			{view}
			doc={openDoc}
			onopen={(keys) => onopen(keys, view!.datasetParam())}
			onback={() => (openDoc = null)}
		/>
	{:else}
		<!-- Page header at the estate's scale: text-lg title + text-sm lede, the same pairing the
		     data and admin index pages open with. -->
		<div class="flex shrink-0 flex-wrap items-center gap-3">
			<div>
				<h1 class="text-lg font-semibold">Annotate</h1>
				<p class="text-muted-foreground text-sm">
					Pick a document, then a chunk — or review a whole document.
				</p>
			</div>
			{#if datasets.length > 1 && view}
				<div class="ml-auto w-56">
					<Select
						options={datasets.map((d) => ({
	value: d.id,
	label: d.docs != null ? `${d.id} (${d.docs} docs)` : d.id,
}))}
						bind:value={datasetChoice}
						ariaLabel="Dataset"
					/>
				</div>
			{:else if view}
				<span class="text-muted-foreground ml-auto font-mono text-xs" data-testid="dataset-id">
					{view.id}
				</span>
			{/if}
		</div>

		{#if error}
			<div class="flex flex-col items-start gap-2">
				<p class="text-destructive text-sm" data-testid="selection-error">{error}</p>
				<Button
					variant="outline"
					size="sm"
					data-testid="selection-retry"
					onclick={() => void retry()}
				>
					Retry
				</Button>
			</div>
		{:else if docsPage === null}
			<p class="text-muted-foreground text-sm">Loading datasets…</p>
		{:else}
			<div
				class="grid min-h-0 flex-1 auto-rows-min grid-cols-[repeat(auto-fill,minmax(13rem,1fr))] gap-3 overflow-y-auto pr-1"
			>
				{#each docsPage.docs as doc (view!.docId(doc))}
					<DocTile view={view!} {doc} onclick={() => (openDoc = doc)} />
				{/each}
			</div>
			<!-- Pagination reads like the estate's DataTable footer: outline prev/next flanking a
			     muted, tabular page counter. -->
			<div class="flex shrink-0 items-center justify-center gap-3">
				<Button
					variant="outline"
					size="sm"
					disabled={loadingDocs || docsPage.page <= 1}
					onclick={() => void loadDocs(docsPage!.page - 1)}
				>
					<ChevronLeft class="size-3.5" /> Previous
				</Button>
				<span class="text-muted-foreground text-xs tabular-nums">
					page {docsPage.page} / {totalPages} · {docsPage.total} documents
				</span>
				<Button
					variant="outline"
					size="sm"
					disabled={loadingDocs || docsPage.page >= totalPages}
					onclick={() => void loadDocs(docsPage!.page + 1)}
				>
					Next <ChevronRight class="size-3.5" />
				</Button>
			</div>
		{/if}
	{/if}
</div>
