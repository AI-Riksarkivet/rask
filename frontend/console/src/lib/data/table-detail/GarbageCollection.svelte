<script lang="ts">
	// #75 on-demand GC, the second half of the maintenance section: preview the versions a retention
	// bound would reclaim (a dry run), then reclaim them behind a two-click confirm. Owner-gated
	// (can_drop) at the catalog. Its bounds, preview and armed confirm are its own; the parent mounts it
	// under the detail page's `{#key table}`, so none of them survives a navigation.
	import { Trash2 } from '@lucide/svelte';
	import { Button } from '@riksarkivet/ui/button';
	import { Input } from '@riksarkivet/ui/input';
	import { type GcPreview } from '../catalog';
	import { fmtBytes } from '../history';
	import { previewMaintenance, runMaintenance } from '../remote/catalog.api';

	let { table, onchanged }: { table: string; onchanged: () => Promise<void> } = $props();

	let gcDays = $state<number | null>(null);
	let gcKeep = $state<number | null>(null);
	let gcBusy = $state(false);
	let gcPreview = $state<GcPreview | null>(null);
	let gcResult = $state<string | null>(null);
	let gcError = $state<string | null>(null);
	let gcConfirm = $state(false);
	const gcHasBound = $derived(gcDays != null || gcKeep != null);

	function gcFail(status: number, detail: string): void {
		if (status === 401) gcError = 'Sign in to run garbage collection.';
		else if (status === 403) gcError = 'Denied: GC needs the modifier rung (can_drop).';
		else if (status === 422) gcError = 'Set a retention-days and/or keep-last bound first.';
		else gcError = detail;
	}

	async function runGcPreview(): Promise<void> {
		if (gcBusy || !gcHasBound) return;
		gcBusy = true;
		gcError = null;
		gcResult = null;
		gcConfirm = false;
		try {
			const res = await previewMaintenance({
				table,
				bounds: { retention_days: gcDays, retain_versions: gcKeep },
			});
			if (res.ok) gcPreview = res.data;
			else gcFail(res.status, res.detail);
		} finally {
			gcBusy = false;
		}
	}

	async function runGc(): Promise<void> {
		if (gcBusy || !gcHasBound) return;
		gcBusy = true;
		gcError = null;
		try {
			const res = await runMaintenance({
				table,
				bounds: { retention_days: gcDays, retain_versions: gcKeep },
			});
			if (res.ok) {
				gcResult = `Reclaimed ${res.data.old_versions_removed} version(s) · ${fmtBytes(res.data.bytes_removed)}.`;
				gcPreview = null;
				gcConfirm = false;
				await onchanged(); // GC changed the version set — refresh
			} else gcFail(res.status, res.detail);
		} finally {
			gcBusy = false;
		}
	}
</script>

<div class="gc">
	<h3>Garbage collection</h3>
	<div class="row">
		<label
			>older than (days) <Input
				class="h-7 w-28 font-mono text-xs"
				type="number"
				min="1"
				bind:value={gcDays}
				placeholder="any age"
			/></label
		>
		<label
			>keep last <Input
				class="h-7 w-28 font-mono text-xs"
				type="number"
				min="1"
				bind:value={gcKeep}
				placeholder="—"
			/></label
		>
		<Button
			variant="ghost"
			size="sm"
			class="self-end"
			disabled={gcBusy || !gcHasBound}
			onclick={runGcPreview}
		>
			Preview
		</Button>
	</div>
	{#if gcPreview}
		<p class="mut">
			{gcPreview.eligible_versions.length} version{gcPreview.eligible_versions.length === 1
				? ''
				: 's'}
			reclaimable
			{#if gcPreview.eligible_versions.length}(v{gcPreview.eligible_versions.join(', v')}){/if}
			· {gcPreview.total_versions} total, current v{gcPreview.current_version}.
			{#if Object.keys(gcPreview.protected_tags).length}
				Protected by tags: {Object.entries(gcPreview.protected_tags)
					.map(([t, v]) => `${t}→v${v}`)
					.join(', ')}.
			{/if}
		</p>
		{#if gcPreview.eligible_versions.length}
			{#if gcConfirm}
				<div class="row items-center">
					<span class="mut"
						>Permanently reclaim {gcPreview.eligible_versions.length} version(s)?</span
					>
					<Button variant="destructive" size="sm" disabled={gcBusy} onclick={runGc}
						>Confirm reclaim</Button
					>
					<Button variant="ghost" size="sm" onclick={() => (gcConfirm = false)}>Cancel</Button>
				</div>
			{:else}
				<Button size="sm" disabled={gcBusy} onclick={() => (gcConfirm = true)}>
					<Trash2 size={12} /> Reclaim now
				</Button>
			{/if}
		{/if}
	{/if}
	{#if gcResult}<p class="mut">{gcResult}</p>{/if}
	{#if gcError}<p class="error">{gcError}</p>{/if}
</div>

<style>
	.gc {
		margin-top: 14px;
		padding-top: 12px;
		border-top: 1px solid color-mix(in srgb, var(--border) 45%, transparent);
	}
	.gc h3 {
		font-size: 13px;
		margin: 0 0 8px;
	}
	.gc label {
		display: inline-flex;
		flex-direction: column;
		gap: 3px;
		font-size: 12px;
		color: var(--muted-foreground);
	}
	.row {
		display: flex;
		gap: 8px;
	}
	.mut {
		color: var(--muted-foreground);
		font-size: 12px;
	}
	.error {
		color: var(--destructive);
		font-size: 12px;
	}
</style>
