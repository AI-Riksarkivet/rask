<!-- The app build (and svelte-check) compiles this WITHOUT customElement: true — correct, the app
     never mounts the wrapper; only vite.elements.config.ts compiles it as an element. -->
<!-- svelte-ignore options_missing_custom_element -->
<svelte:options customElement={{ shadow: 'none' }} />

<script lang="ts">
	/**
	 * `<rask-lakehouse-storage>` — the S3 object browser, served by the lakehouse zone to the global
	 * workbench. Mirrors `/lakehouse/catalog/storage` (ObjectBrowser): the store picker, the prefix
	 * breadcrumb, folder-then-file rows with size and modified stamps.
	 *
	 * Transport: BOTH reads are root-absolute GETs the zone already proxies —
	 * `/lakehouse/capi/v1/stores` (catalog, session bearer attached by the proxy) and
	 * `/lakehouse/api/media/objects?bucket=&prefix=` (the lineage/media object lister). The zone page
	 * reaches the first through a remote function; an element cannot import one, so it calls the same
	 * upstream through the GET-only proxy. Formatting helpers are re-implemented here rather than
	 * imported from `$lib/storage/storage.ts`, which pulls the zone's `$app`-bound http client.
	 */
	import { Card } from '@rask/ui/card';
	import { Badge } from '@rask/ui/badge';
	import { File, Folder } from '@lucide/svelte';
	import { RASK_SELECT, type SelectDetail } from '@rask/dockview/contract';
	import { ElementPoll } from './poll.svelte';

	type Store = {
		name: string;
		bucket: string;
		role: string;
		description: string;
		read_only: boolean;
	};
	type Listing = {
		prefixes?: { prefix: string }[];
		objects?: { key: string; size: number | null; last_modified: string | null }[];
	};

	let { pollms = 60000, filtertext = '' }: { pollms?: number; filtertext?: string } = $props();

	/** The store registry — the picker's source. */
	const stores = new ElementPoll<Store[]>(async (f) => {
		const res = await f('/lakehouse/capi/v1/stores');
		if (res.status === 401) throw new Error('session expired or no access');
		if (!res.ok) throw new Error(`HTTP ${res.status}`);
		const body = (await res.json()) as { stores?: Store[] };
		return body.stores ?? [];
	});
	$effect(() => stores.start(pollms));

	let picked = $state('');
	let prefix = $state('');
	const store = $derived(
		(stores.data ?? []).find((s) => s.name === picked) ?? (stores.data ?? [])[0],
	);

	let listing = $state<Listing | null>(null);
	let listError = $state<string | null>(null);
	let listing_busy = $state(false);

	$effect(() => {
		const bucket = store?.bucket;
		const at = prefix;
		if (bucket === undefined) return;
		let live = true;
		listing_busy = true;
		(async () => {
			try {
				const res = await fetch(
					`/lakehouse/api/media/objects?bucket=${encodeURIComponent(bucket)}&prefix=${encodeURIComponent(at)}`,
					{ signal: AbortSignal.timeout(8000) },
				);
				if (!live) return;
				if (res.status === 401) throw new Error('session expired or no access');
				if (!res.ok) throw new Error(`HTTP ${res.status}`);
				listing = (await res.json()) as Listing;
				listError = null;
			} catch (e) {
				if (live) listError = e instanceof Error ? e.message : String(e);
			} finally {
				if (live) listing_busy = false;
			}
		})();
		return () => {
			live = false;
		};
	});

	const folders = $derived(
		(listing?.prefixes ?? []).filter(
			(p) => filtertext.trim() === '' || p.prefix.toLowerCase().includes(filtertext.toLowerCase()),
		),
	);
	const files = $derived(
		(listing?.objects ?? []).filter(
			(o) => filtertext.trim() === '' || o.key.toLowerCase().includes(filtertext.toLowerCase()),
		),
	);

	/** Mirrors ObjectBrowser's fmtSize/fmtModified — same units, same em-dash fallbacks. */
	function fmtSize(bytes: number | null): string {
		if (bytes === null) return '—';
		const units = ['B', 'KB', 'MB', 'GB', 'TB'];
		let n = bytes;
		let i = 0;
		while (n >= 1024 && i < units.length - 1) {
			n /= 1024;
			i += 1;
		}
		return `${n < 10 && i > 0 ? n.toFixed(1) : Math.round(n)} ${units[i]}`;
	}
	function fmtModified(iso: string | null): string {
		if (iso === null) return '—';
		return iso.replace('T', ' ').slice(0, 19);
	}
	const leaf = (key: string): string => key.slice(prefix.length).replace(/\/$/, '');

	const crumbs = $derived(
		prefix === '' ? [] : prefix.replace(/\/$/, '').split('/').filter(Boolean),
	);

	function select(node: HTMLElement, key: string, kind: 'object' | 'prefix') {
		node.dispatchEvent(
			new CustomEvent(RASK_SELECT, {
				bubbles: true,
				composed: true,
				detail: {
					source: 'rask-lakehouse-storage',
					kind: kind === 'object' ? 'storage-object' : 'storage-prefix',
					id: `${store?.bucket ?? ''}/${key}`,
					label: leaf(key) || key,
				} satisfies SelectDetail,
			}),
		);
	}
</script>

<div class="bg-background block h-full overflow-auto p-3">
	<p class="text-muted-foreground mb-2 text-[11px]">
		mounted {stores.mountedAt} · poll #{stores.polls}
	</p>

	{#if stores.error !== null}
		<p class="text-destructive text-sm">Store registry unavailable: {stores.error}</p>
	{:else if (stores.data ?? []).length === 0}
		<p class="text-muted-foreground text-sm">No stores registered.</p>
	{:else}
		<div class="mb-2 flex flex-wrap items-center gap-2">
			<select
				class="border-border bg-background text-foreground rounded-md border px-2 py-1 text-xs"
				aria-label="Store"
				bind:value={picked}
				onchange={() => (prefix = '')}
			>
				{#each stores.data ?? [] as s (s.name)}
					<option value={s.name}>{s.name} · {s.bucket}</option>
				{/each}
			</select>
			{#if store}
				<Badge variant="secondary">{store.role}</Badge>
				{#if store.read_only}<Badge variant="outline">read-only</Badge>{/if}
			{/if}
			<nav class="text-muted-foreground flex items-center gap-1 text-xs">
				<button type="button" class="hover:text-foreground" onclick={() => (prefix = '')}>/</button>
				{#each crumbs as part, i (i)}
					<span>/</span>
					<button
						type="button"
						class="hover:text-foreground"
						onclick={() => (prefix = crumbs.slice(0, i + 1).join('/') + '/')}>{part}</button
					>
				{/each}
			</nav>
		</div>

		{#if listError !== null}
			<p class="text-destructive text-sm">Objects unavailable: {listError}</p>
		{:else if listing_busy && listing === null}
			<p class="text-muted-foreground text-sm">Listing…</p>
		{:else if folders.length === 0 && files.length === 0}
			<p class="text-muted-foreground text-sm">Nothing at this prefix.</p>
		{:else}
			<Card class="overflow-hidden">
				<div class="max-h-full overflow-auto">
					<table class="w-full border-collapse text-xs">
						<thead class="bg-muted/50 sticky top-0 text-left">
							<tr class="border-b">
								<th class="text-muted-foreground px-3 py-2 font-medium">name</th>
								<th class="text-muted-foreground px-3 py-2 font-medium">size</th>
								<th class="text-muted-foreground px-3 py-2 font-medium">modified</th>
							</tr>
						</thead>
						<tbody>
							{#each folders as f (f.prefix)}
								<tr
									class="hover:bg-muted/50 cursor-pointer border-b transition-colors"
									onclick={(e) => {
	select(e.currentTarget, f.prefix, 'prefix');
	prefix = f.prefix;
}}
								>
									<td class="px-3 py-2 align-middle whitespace-nowrap">
										<span class="flex items-center gap-2 font-mono"><Folder size={13} />{leaf(f.prefix)}</span
										>
									</td>
									<td class="text-muted-foreground px-3 py-2">—</td>
									<td class="text-muted-foreground px-3 py-2">—</td>
								</tr>
							{/each}
							{#each files as o (o.key)}
								<tr
									class="hover:bg-muted/50 cursor-pointer border-b transition-colors"
									onclick={(e) => select(e.currentTarget, o.key, 'object')}
								>
									<td class="px-3 py-2 align-middle whitespace-nowrap">
										<span class="flex items-center gap-2 font-mono"><File size={13} />{leaf(o.key)}</span>
									</td>
									<td class="px-3 py-2 font-mono tabular-nums">{fmtSize(o.size)}</td>
									<td class="text-muted-foreground px-3 py-2 font-mono">{fmtModified(o.last_modified)}</td>
								</tr>
							{/each}
						</tbody>
					</table>
				</div>
			</Card>
		{/if}
	{/if}
</div>
