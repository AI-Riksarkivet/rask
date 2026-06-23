<script lang="ts">
	import { onMount } from 'svelte';
	import { base } from '$app/paths';
	import { browseCatalog, type CatalogHit, type CatalogTier } from '@rask/api';
	import { Card } from '@rask/ui/card';
	import { Badge } from '@rask/ui/badge';
	import { Loader2, ExternalLink, Search } from '@lucide/svelte';

	// Single fetch — at the server cap, 2000 covers our current 715 cached
	// volumes with plenty of headroom. If we ever exceed this, we'll need
	// to either lift the cap or page on archive_code.
	const FETCH_LIMIT = 2000;

	let tier = $state<CatalogTier>('cached');
	let hits = $state<CatalogHit[]>([]);
	let total = $state(0);
	let loading = $state(false);
	let error = $state<string | null>(null);

	async function load() {
		loading = true;
		error = null;
		try {
			const r = await browseCatalog(tier, FETCH_LIMIT, 0);
			hits = r.hits;
			total = r.total;
		} catch (err) {
			error = err instanceof Error ? err.message : String(err);
			hits = [];
		} finally {
			loading = false;
		}
	}

	onMount(load);
	$effect(() => {
		tier;
		load();
	});

	// Group hits into a 3-level tree:
	//   archive_code  ->  fonds_id  ->  series_id  ->  [volumes]
	// `fondsTitle` and `seriesTitle` are remembered alongside each branch
	// so the display can show human labels without a second pass.
	type SeriesGroup = { id: string; title: string; volumes: CatalogHit[] };
	type FondsGroup = { id: string; title: string; series: SeriesGroup[] };
	type ArchiveGroup = { code: string; fonds: FondsGroup[]; total: number };

	const tree = $derived.by<ArchiveGroup[]>(() => {
		const byArchive = new Map<
			string,
			Map<string, { title: string; series: Map<string, { title: string; volumes: CatalogHit[] }> }>
		>();
		for (const h of hits) {
			const arch = h.archive_code || '(unknown)';
			const fondsId = h.fonds_id || '(no fonds)';
			const seriesId = h.series_id || '(no series)';
			let fondsMap = byArchive.get(arch);
			if (!fondsMap) {
				fondsMap = new Map();
				byArchive.set(arch, fondsMap);
			}
			let fEntry = fondsMap.get(fondsId);
			if (!fEntry) {
				fEntry = { title: h.fonds_title || fondsId, series: new Map() };
				fondsMap.set(fondsId, fEntry);
			}
			let sEntry = fEntry.series.get(seriesId);
			if (!sEntry) {
				sEntry = { title: h.series_title || seriesId, volumes: [] };
				fEntry.series.set(seriesId, sEntry);
			}
			sEntry.volumes.push(h);
		}
		// Convert maps to sorted arrays. Archives: alpha by code. Fonds:
		// alpha by title. Series: alpha by id (preserves original order
		// for compound codes like "A 1 A"). Volumes: by date_start then
		// volume_id, fallback to bild_id.
		const out: ArchiveGroup[] = [];
		for (const [code, fondsMap] of [...byArchive.entries()].sort((a, b) =>
			a[0].localeCompare(b[0]),
		)) {
			let archTotal = 0;
			const fondsList: FondsGroup[] = [];
			for (const [fondsId, fEntry] of [...fondsMap.entries()].sort((a, b) =>
				(a[1].title || a[0]).localeCompare(b[1].title || b[0]),
			)) {
				const seriesList: SeriesGroup[] = [];
				for (const [seriesId, sEntry] of [...fEntry.series.entries()].sort((a, b) =>
					a[0].localeCompare(b[0]),
				)) {
					sEntry.volumes.sort((a, b) => {
						const da = a.date_start ?? 0;
						const db = b.date_start ?? 0;
						if (da !== db) return da - db;
						return (a.volume_id || a.bild_id).localeCompare(b.volume_id || b.bild_id);
					});
					seriesList.push({ id: seriesId, title: sEntry.title, volumes: sEntry.volumes });
					archTotal += sEntry.volumes.length;
				}
				fondsList.push({ id: fondsId, title: fEntry.title, series: seriesList });
			}
			out.push({ code, fonds: fondsList, total: archTotal });
		}
		return out;
	});

	// Cached-or-better volumes open locally; listed-only ones go to Riksarkivet.
	function hrefFor(h: CatalogHit): { href: string; external: boolean } {
		if (h.cached)
			return {
				href: `${base}/viewer/${encodeURIComponent(h.bild_id)}`,
				external: false,
			};
		return { href: h.bildvisning_url || '#', external: true };
	}

	function fmtDate(h: CatalogHit): string {
		if (h.date_text) return h.date_text;
		if (h.date_start && h.date_end) {
			return h.date_start === h.date_end ? String(h.date_start) : `${h.date_start}–${h.date_end}`;
		}
		return '';
	}

	function fondsCount(f: FondsGroup): number {
		return f.series.reduce((n, s) => n + s.volumes.length, 0);
	}
</script>

<svelte:head>
	<title>Browse — RASK</title>
</svelte:head>

<main class="bg-background flex-1 overflow-auto">
	<div class="flex flex-col gap-4 p-6 text-sm">
		<div class="flex flex-wrap items-center gap-3">
			<label class="flex cursor-pointer items-center gap-1.5 select-none">
				<span class="text-muted-foreground">Show:</span>
				<select
					bind:value={tier}
					class="border-input bg-background cursor-pointer rounded-md border px-2 py-1 text-sm"
				>
					<option value="listed">listed</option>
					<option value="cached">cached</option>
					<option value="transcribed">transcribed</option>
				</select>
			</label>
			<Badge variant="secondary">{total.toLocaleString()} {tier} volumes</Badge>
			{#if hits.length < total}
				<span class="text-muted-foreground text-xs"
					>showing first {hits.length.toLocaleString()} (limit {FETCH_LIMIT.toLocaleString()})</span
				>
			{/if}
			{#if loading}
				<Loader2 class="text-muted-foreground h-4 w-4 animate-spin" />
			{/if}
		</div>

		{#if error}
			<Card class="border-destructive/40 bg-destructive/10 text-destructive p-3">{error}</Card>
		{/if}

		{#if tree.length > 0}
			<!-- Native <details>/<summary> for the tree expansion — keyboard
			     accessible, no JS state to track per node, and the browser
			     remembers expanded state on back-nav. Indentation grows by
			     2rem per level. -->
			<div class="flex flex-col gap-1">
				{#each tree as arch (arch.code)}
					<details class="bg-card rounded-md border">
						<summary
							class="hover:bg-accent/30 flex cursor-pointer items-center gap-2 px-3 py-2 font-medium"
						>
							<span>{arch.code}</span>
							<span class="text-muted-foreground text-xs">
								{arch.fonds.length} fonds · {arch.total} volumes
							</span>
						</summary>
						<div class="flex flex-col gap-1 px-3 py-1">
							{#each arch.fonds as f (f.id)}
								<details class="border-border/40 rounded border">
									<summary
										class="hover:bg-accent/30 flex cursor-pointer flex-wrap items-baseline gap-2 px-3 py-1.5 text-sm"
									>
										<span class="font-medium">{f.title}</span>
										<span class="text-muted-foreground font-mono text-xs">{f.id}</span>
										<span class="text-muted-foreground text-xs">· {fondsCount(f)} volumes</span>
									</summary>
									<div class="flex flex-col gap-1 px-3 py-1">
										{#each f.series as s (s.id)}
											<details class="border-border/40 bg-muted/20 rounded border">
												<summary
													class="hover:bg-accent/30 flex cursor-pointer flex-wrap items-baseline gap-2 px-3 py-1.5 text-xs"
												>
													<span class="font-medium">{s.title}</span>
													<span class="text-muted-foreground font-mono">{s.id}</span>
													<span class="text-muted-foreground">· {s.volumes.length}</span>
												</summary>
												<ul class="flex flex-col">
													{#each s.volumes as v (v.id)}
														{@const link = hrefFor(v)}
														<li>
															<a
																href={link.href}
																target={link.external ? '_blank' : undefined}
																rel={link.external ? 'noopener' : undefined}
																class="border-border/40 hover:bg-accent/30 flex items-center gap-3 border-t px-3 py-1.5 text-xs transition"
															>
																<span class="w-24 shrink-0 font-mono">{v.bild_id}</span>
																<span class="min-w-0 flex-1 truncate">
																	{v.volume_title || '(no title)'}
																	{#if fmtDate(v) && fmtDate(v) !== v.volume_title}
																		<span class="text-muted-foreground">· {fmtDate(v)}</span>
																	{/if}
																</span>
																{#if v.transcribed}
																	<Badge
																		class="bg-emerald-500/20 text-[10px] text-emerald-700 dark:text-emerald-300"
																	>
																		T
																	</Badge>
																{:else if v.cached}
																	<Badge
																		class="bg-blue-500/20 text-[10px] text-blue-700 dark:text-blue-300"
																	>
																		C
																	</Badge>
																{:else}
																	<Badge
																		class="bg-amber-500/20 text-[10px] text-amber-700 dark:text-amber-300"
																	>
																		L
																	</Badge>
																{/if}
																{#if link.external}
																	<ExternalLink class="text-muted-foreground h-3 w-3 shrink-0" />
																{:else}
																	<Search class="h-3 w-3 shrink-0 text-emerald-600" />
																{/if}
															</a>
														</li>
													{/each}
												</ul>
											</details>
										{/each}
									</div>
								</details>
							{/each}
						</div>
					</details>
				{/each}
			</div>
		{:else if !loading}
			<Card class="text-muted-foreground p-6 text-center">
				No <span class="font-medium">{tier}</span> volumes yet.
			</Card>
		{/if}
	</div>
</main>
