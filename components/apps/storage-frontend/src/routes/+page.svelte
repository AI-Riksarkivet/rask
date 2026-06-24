<script lang="ts">
	import { Badge } from '@rask/ui/badge';
	import { Button } from '@rask/ui/button';
	import { Dialog } from '@rask/ui/dialog';
	import { listObjects, headObject } from '$lib/remote/storage.remote';
	import { BUCKETS, type Bucket } from '$lib/storage';
	import {
		Database,
		Folder,
		FileText,
		ChevronRight,
		House,
		TriangleAlert,
		Download,
	} from '@lucide/svelte';

	let bucket = $state<Bucket>(BUCKETS[0]);
	let prefix = $state('');

	// The object whose detail dialog is open (null = closed). Read-only browse:
	// the gateway's volumes-api is backend-agnostic, so this works against
	// MinIO/AWS/HCP unchanged.
	let detailKey = $state<string | null>(null);
	const detailOpen = $derived(detailKey !== null);

	// THE ONE PATTERN (see lib/remote/storage.remote.ts): cache the query handle,
	// keyed on reactive params via `$derived`, so navigation re-keys + re-runs it
	// (re-calling listObjects() in markup would defeat the cache). `await`-ed in a
	// `<svelte:boundary>` below — first paint already has the data (experimental.async).
	const listingQuery = $derived(listObjects({ bucket, prefix }));
	// Object HEAD metadata, lazily keyed on the open key (null = dialog closed).
	const headQuery = $derived(detailKey ? headObject({ bucket, key: detailKey }) : null);

	const segments = $derived(prefix.split('/').filter(Boolean));

	function openPrefix(p: string) {
		prefix = p;
	}
	function crumbTo(idx: number) {
		prefix = segments.slice(0, idx + 1).join('/') + '/';
	}
	function fmtSize(bytes: number): string {
		if (bytes < 1024) return `${bytes} B`;
		if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
		return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
	}
	function leaf(p: string): string {
		return p.replace(/\/$/, '').split('/').pop() ?? p;
	}
	/** Client-side download via the gateway proxy (Content-Disposition: attachment). */
	function downloadHref(key: string): string {
		const params = new URLSearchParams({ bucket, key });
		return `/api/volumes/object/download?${params}`;
	}
	function fmtDate(iso: string | null): string {
		if (!iso) return '—';
		const d = new Date(iso);
		return Number.isNaN(d.getTime()) ? iso : d.toLocaleString();
	}
</script>

<div class="mx-auto flex w-full max-w-4xl flex-col gap-4 p-6">
	<div class="flex items-center gap-2">
		<Database class="text-muted-foreground h-5 w-5" />
		{#each BUCKETS as b (b)}
			<button
				class="rounded-md border px-3 py-1 text-sm {b === bucket
					? 'bg-primary text-primary-foreground'
					: 'hover:bg-accent'}"
				onclick={() => {
					bucket = b;
					prefix = '';
				}}
			>
				{b}
			</button>
		{/each}
	</div>

	<div class="text-muted-foreground flex flex-wrap items-center gap-1 text-sm">
		<button class="hover:text-foreground flex items-center gap-1" onclick={() => openPrefix('')}>
			<House class="h-3.5 w-3.5" />
			{bucket}
		</button>
		{#each segments as seg, i (segments.slice(0, i + 1).join('/'))}
			<ChevronRight class="h-3.5 w-3.5" />
			<button class="hover:text-foreground" onclick={() => crumbTo(i)}>{seg}</button>
		{/each}
	</div>

	<div class="bg-card overflow-hidden rounded-lg border">
		<svelte:boundary>
			{#snippet pending()}
				<div class="text-muted-foreground p-8 text-center text-sm">Loading {bucket}…</div>
			{/snippet}

			{#snippet failed(boundaryError, reset)}
				<div class="flex flex-col items-center gap-2 p-8 text-center">
					<TriangleAlert class="h-8 w-8 text-amber-500" />
					<p class="text-sm font-medium">Couldn't load objects</p>
					<p class="text-muted-foreground max-w-md text-xs">
						{boundaryError instanceof Error ? boundaryError.message : String(boundaryError)}
					</p>
					<Badge variant="warning">{bucket}</Badge>
					<Button size="sm" variant="outline" onclick={reset}>Retry</Button>
				</div>
			{/snippet}

			{@const listing = await listingQuery}
			{#if listing.prefixes.length === 0 && listing.objects.length === 0}
				<div class="text-muted-foreground p-8 text-center text-sm">Empty prefix.</div>
			{:else}
				<table class="w-full text-sm">
					<thead class="text-muted-foreground border-b text-xs">
						<tr>
							<th class="px-4 py-2 text-left font-medium">Name</th>
							<th class="px-4 py-2 text-right font-medium">Size</th>
							<th class="w-10 px-4 py-2"></th>
						</tr>
					</thead>
					<tbody>
						{#each listing.prefixes as p (p)}
							<tr class="hover:bg-accent/40 border-b last:border-0">
								<td class="px-4 py-2">
									<button class="flex items-center gap-2 font-medium" onclick={() => openPrefix(p)}>
										<Folder class="h-4 w-4 text-amber-500" />
										{leaf(p)}/
									</button>
								</td>
								<td class="text-muted-foreground px-4 py-2 text-right">—</td>
								<td></td>
							</tr>
						{/each}
						{#each listing.objects as obj (obj.key)}
							<tr class="hover:bg-accent/40 border-b last:border-0">
								<td class="px-4 py-2">
									<button
										class="hover:text-primary flex items-center gap-2 text-left"
										title="View details"
										onclick={() => (detailKey = obj.key)}
									>
										<FileText class="text-muted-foreground h-4 w-4" />
										{leaf(obj.key)}
									</button>
								</td>
								<td class="text-muted-foreground px-4 py-2 text-right font-mono text-xs">
									{fmtSize(obj.size)}
								</td>
								<td class="px-2 py-2 text-right">
									<a
										class="text-muted-foreground hover:text-foreground inline-flex"
										href={downloadHref(obj.key)}
										title="Download"
										download={leaf(obj.key)}
									>
										<Download class="h-4 w-4" />
									</a>
								</td>
							</tr>
						{/each}
					</tbody>
				</table>
			{/if}
		</svelte:boundary>
	</div>
</div>

<!-- Object-detail dialog: HEAD metadata + a download, lazily fetched when opened. -->
<Dialog.Root open={detailOpen} onOpenChange={(o) => (detailKey = o ? detailKey : null)}>
	<Dialog.Content>
		{#if detailKey}
			{@const key = detailKey}
			<Dialog.Title>{leaf(key)}</Dialog.Title>
			<Dialog.Description class="font-mono text-xs break-all">{key}</Dialog.Description>
			<svelte:boundary>
				{#snippet pending()}
					<div class="text-muted-foreground py-4 text-sm">Loading details…</div>
				{/snippet}

				{#snippet failed(boundaryError, reset)}
					<div class="flex flex-col items-start gap-2 py-4">
						<p class="text-destructive text-sm">
							{boundaryError instanceof Error ? boundaryError.message : String(boundaryError)}
						</p>
						<Button size="sm" variant="outline" onclick={reset}>Retry</Button>
					</div>
				{/snippet}

				{#if headQuery}
					{@const head = await headQuery}
					<dl class="grid grid-cols-[6rem_1fr] gap-y-2 text-sm">
						<dt class="text-muted-foreground">Size</dt>
						<dd class="font-mono">{fmtSize(head.size)}</dd>
						<dt class="text-muted-foreground">Type</dt>
						<dd class="font-mono">{head.content_type ?? '—'}</dd>
						<dt class="text-muted-foreground">Modified</dt>
						<dd>{fmtDate(head.last_modified)}</dd>
						<dt class="text-muted-foreground">ETag</dt>
						<dd class="font-mono text-xs break-all">{head.etag ?? '—'}</dd>
					</dl>
				{/if}
			</svelte:boundary>
			<div class="flex justify-end">
				<Button href={downloadHref(key)} download={leaf(key)}>
					<Download class="h-4 w-4" />
					Download
				</Button>
			</div>
		{/if}
	</Dialog.Content>
</Dialog.Root>
