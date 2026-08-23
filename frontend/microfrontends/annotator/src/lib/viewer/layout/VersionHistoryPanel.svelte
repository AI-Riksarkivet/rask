<script lang="ts">
	// Compare-versions panel (lives inside the sidebar). The read-side of the write-plane
	// provenance story: this unit's edit history (version · time · count) + a diff of any
	// past version against the current rows. Opt-in (an extra fetch), lazy-loaded. All
	// fetch/diff logic is in @rask/labeling/history (framework-agnostic); this is the view.
	import { ChevronDown, ChevronRight, History } from '@lucide/svelte';
	import { cn } from '@rask/ui/utils';
	import {
		diffSignatures,
		fetchVersions,
		fetchVersionSignatures,
		type AnnotationVersion,
		type RowDiff,
	} from '@rask/labeling/history';
	import type { AnnotatorController } from '../annotator.svelte';

	let { controller }: { controller: AnnotatorController } = $props();

	let collapsed = $state(true);
	let versions = $state<AnnotationVersion[]>([]);
	let loading = $state(false);
	let error = $state<string | null>(null);
	let selected = $state<number | null>(null);
	let diff = $state<RowDiff | null>(null);
	let diffing = $state(false);
	let diffError = $state<string | null>(null);

	async function open(): Promise<void> {
		collapsed = false;
		const url = controller.annotationsUrl;
		if (!url || versions.length || loading) return;
		loading = true;
		error = null;
		try {
			versions = await fetchVersions(url);
		} catch (e) {
			error = e instanceof Error ? e.message : 'failed to load history';
		} finally {
			loading = false;
		}
	}

	async function compare(v: number): Promise<void> {
		const url = controller.annotationsUrl;
		if (!url) return;
		if (selected === v) {
			selected = null;
			diff = null;
			diffError = null;
			return;
		}
		selected = v;
		diff = null;
		diffError = null;
		diffing = true;
		try {
			const past = await fetchVersionSignatures(url, v);
			if (selected !== v) return; // a newer selection superseded this fetch (latest-wins)
			diff = diffSignatures(past, controller.currentSignatures);
		} catch (e) {
			if (selected !== v) return;
			diffError = e instanceof Error ? e.message : 'compare failed';
		} finally {
			if (selected === v) diffing = false;
		}
	}

	// "2026-07-20T23:50:10.926427" → "23:50:10"
	const timeOf = (ts: string): string => ts.split('T')[1]?.slice(0, 8) ?? ts;
</script>

<section class="border-border border-t" data-testid="version-history">
	<button
		class="text-muted-foreground hover:text-foreground flex w-full items-center gap-1.5 px-3 py-2 text-xs font-medium transition-colors"
		onclick={() => (collapsed ? open() : (collapsed = true))}
	>
		{#if collapsed}<ChevronRight class="size-3.5" />{:else}<ChevronDown class="size-3.5" />{/if}
		<History class="size-3.5" />
		History
	</button>

	{#if !collapsed}
		<div class="flex flex-col gap-0.5 px-3 pb-3">
			{#if loading}
				<p class="text-muted-foreground text-xs">Loading history…</p>
			{:else if error}
				<p class="text-destructive text-xs">{error}</p>
			{:else if versions.length === 0}
				<p class="text-muted-foreground text-xs">No history for this unit.</p>
			{:else}
				{#each versions as v, i (v.version)}
					<button
						class={cn(
							'hover:bg-muted/50 flex items-center gap-2 rounded-md px-1 py-0.5 text-left transition-colors',
							selected === v.version && 'bg-primary/10 ring-primary/40 ring-1',
						)}
						title="Compare this version against the current rows"
						onclick={() => compare(v.version)}
					>
						<span class="w-8 shrink-0 font-mono text-xs tabular-nums">v{v.version}</span>
						<span class="text-muted-foreground flex-1 truncate font-mono text-xs">
							{timeOf(v.timestamp)}{i === 0 ? ' · current' : ''}
						</span>
						<span
							class="text-muted-foreground text-xs tabular-nums"
							title="annotations at this version"
						>
							{v.count}
						</span>
					</button>
					{#if selected === v.version}
						<!-- Diff counters speak the same semantic tokens as the status dots:
						     success/destructive/warning, not a bespoke emerald-rose-amber trio. -->
						<div class="text-muted-foreground px-2 pb-1 text-xs">
							{#if diffing}
								comparing…
							{:else if diffError}
								<span class="text-destructive">compare failed: {diffError}</span>
							{:else if diff}
								vs current:
								<span class="text-success">+{diff.added.length}</span>
								<span class="text-destructive">−{diff.removed.length}</span>
								<span class="text-warning">~{diff.changed.length}</span>
							{/if}
						</div>
					{/if}
				{/each}
			{/if}
		</div>
	{/if}
</section>
