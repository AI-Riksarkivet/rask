<script lang="ts">
	// The maintenance plane, in one section because its three workflows share state on purpose:
	// the #50 policy (owner-gated set/delete) feeds the #76 compact-now target, and compaction
	// reports its refusals through the same error strip as the #75 on-demand GC (preview → two-click
	// destructive reclaim) — one door, one wording, per the original page.
	//
	// Split out of TableDetail.svelte (#98). Mounted under the parent's `{#key table}`, so the
	// instance never survives a navigation — an open editor or an armed GC confirm dies with it.
	import { Trash2 } from '@lucide/svelte';
	import { type GcPreview, type Policy } from '../catalog';
	import { fmtBytes } from '../history';
	import { policyRequestFrom } from '../namespace';
	import {
		compactTable,
		deleteTablePolicy,
		previewMaintenance,
		runMaintenance,
		setTablePolicy,
	} from '../remote/catalog.remote';

	let {
		table,
		policy,
		policyUnavailable,
		onchanged,
	}: {
		table: string;
		policy: Policy | null;
		policyUnavailable: boolean;
		onchanged: () => Promise<void>;
	} = $props();

	let busy = $state(false);
	let policyError = $state<string | null>(null);
	let editingPolicy = $state(false);
	// number | null, matching what bind:value on a type="number" input actually delivers (Svelte 5
	// coerces to a number, or null for an empty field) — typing them as string crashed savePolicy's
	// guards the moment the user touched a field (audit 2026-07-16).
	let draft = $state<{
		retention_days: number | null;
		retain_versions: number | null;
		interval: number | null;
		target: number | null; // #76 target_rows_per_fragment
		enabled: boolean;
	}>({ retention_days: null, retain_versions: null, interval: null, target: null, enabled: true });

	function startPolicyEdit(): void {
		draft = {
			retention_days: policy?.retention_days ?? null,
			retain_versions: policy?.retain_versions ?? null,
			interval: policy?.compact_interval_hours ?? null,
			target: policy?.target_rows_per_fragment ?? null,
			enabled: policy?.compact_enabled ?? true,
		};
		policyError = null;
		editingPolicy = true;
	}

	function policyFail(status: number, detailText: string): void {
		if (status === 401) policyError = 'Sign in to edit the maintenance policy.';
		else if (status === 403) policyError = 'Denied: policy changes need the owner rung (can_drop).';
		else policyError = detailText;
	}

	async function savePolicy(): Promise<void> {
		if (busy) return;
		busy = true;
		policyError = null;
		try {
			const body = policyRequestFrom(draft);
			const res = await setTablePolicy({ table, policy: body });
			if (res.ok) {
				editingPolicy = false;
				await onchanged();
			} else {
				policyFail(res.status, res.detail);
			}
		} finally {
			busy = false;
		}
	}

	async function removePolicy(): Promise<void> {
		if (busy) return;
		busy = true;
		policyError = null;
		try {
			const res = await deleteTablePolicy({ table });
			if (res.ok) await onchanged();
			else policyFail(res.status, res.detail);
		} finally {
			busy = false;
		}
	}

	// #75 on-demand GC — preview (dry-run reclaimable versions) + run (destructive, two-click confirm).
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
		else if (status === 403) gcError = 'Denied: GC needs the owner rung (can_drop).';
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

	// #76 compact-now — merge small fragments (non-destructive), using the policy's target size if set.
	let compactBusy = $state(false);
	let compactResult = $state<string | null>(null);

	async function runCompact(): Promise<void> {
		if (compactBusy) return;
		compactBusy = true;
		gcError = null;
		compactResult = null;
		try {
			const res = await compactTable({
				table,
				targetRowsPerFragment: policy?.target_rows_per_fragment ?? null,
			});
			if (res.ok) {
				compactResult = `Compacted · ${res.data.fragments_removed} fragment(s) → ${res.data.fragments_added}.`;
				await onchanged(); // compaction wrote a new version — refresh
			} else gcFail(res.status, res.detail);
		} finally {
			compactBusy = false;
		}
	}
</script>

<section>
	<h2>Maintenance policy</h2>
	{#if editingPolicy}
		<div class="policy-edit">
			<label
				>retention days <input
					class="mono"
					type="number"
					min="1"
					bind:value={draft.retention_days}
					placeholder="global default"
				/></label
			>
			<label
				>retain versions <input
					class="mono"
					type="number"
					min="1"
					bind:value={draft.retain_versions}
					placeholder="—"
				/></label
			>
			<label
				>compact every (h) <input
					class="mono"
					type="number"
					min="1"
					bind:value={draft.interval}
					placeholder="every sweep"
				/></label
			>
			<label
				>target rows/fragment <input
					class="mono"
					type="number"
					min="1024"
					bind:value={draft.target}
					placeholder="Lance default"
				/></label
			>
			<label class="check"
				><input type="checkbox" bind:checked={draft.enabled} /> maintenance enabled</label
			>
			<div class="row">
				<button class="btn" disabled={busy} onclick={savePolicy}>Save policy</button>
				<button class="btn ghost" onclick={() => (editingPolicy = false)}>Cancel</button>
			</div>
		</div>
	{:else if policyUnavailable}
		<p class="mut">
			Policy unavailable right now — not shown to avoid an overwriting edit against a stale read.
		</p>
	{:else if policy}
		<div class="refs">
			{#if policy.retention_days}<span class="chip mono">retention {policy.retention_days}d</span>{/if}
			{#if policy.retain_versions}<span class="chip mono">keep last {policy.retain_versions}</span>{/if}
			{#if policy.compact_interval_hours}<span class="chip mono">every {policy.compact_interval_hours}h</span>{/if}
			{#if policy.target_rows_per_fragment}<span class="chip mono">target {policy.target_rows_per_fragment}
			rows/frag</span>{/if}
			{#if !policy.compact_enabled}<span class="chip off mono">maintenance off</span>{/if}
			<button class="btn ghost" onclick={startPolicyEdit}>Edit</button>
			<button class="btn ghost danger" disabled={busy} onclick={removePolicy}>
				<Trash2 size={12} /> Remove
			</button>
		</div>
		<p class="mut">
			Enforced by the compaction sweep; tag-pinned versions (e.g. blessed) are never cleaned up.
		</p>
	{:else}
		<p class="mut">
			No policy — the sweep applies the global defaults.
			<button class="btn ghost" onclick={startPolicyEdit}>Set policy</button>
		</p>
	{/if}
	{#if policyError}<p class="error">{policyError}</p>{/if}

	<!-- #75 on-demand GC: dry-run reclaimable versions, then reclaim (owner-gated, destructive). -->
	<div class="gc">
		<h3>Garbage collection</h3>
		<div class="row">
			<label
				>older than (days) <input
					class="mono"
					type="number"
					min="1"
					bind:value={gcDays}
					placeholder="any age"
				/></label
			>
			<label
				>keep last <input
					class="mono"
					type="number"
					min="1"
					bind:value={gcKeep}
					placeholder="—"
				/></label
			>
			<button class="btn ghost" disabled={gcBusy || !gcHasBound} onclick={runGcPreview}>
				Preview
			</button>
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
					<div class="row">
						<span class="mut"
							>Permanently reclaim {gcPreview.eligible_versions.length} version(s)?</span
						>
						<button class="btn danger" disabled={gcBusy} onclick={runGc}>Confirm reclaim</button>
						<button class="btn ghost" onclick={() => (gcConfirm = false)}>Cancel</button>
					</div>
				{:else}
					<button class="btn" disabled={gcBusy} onclick={() => (gcConfirm = true)}>
						<Trash2 size={12} /> Reclaim now
					</button>
				{/if}
			{/if}
		{/if}
		{#if gcResult}<p class="mut">{gcResult}</p>{/if}

		<!-- #76 compact-now: merge small fragments (non-destructive), using the policy's target size. -->
		<div class="row gc-compact">
			<button class="btn ghost" disabled={compactBusy} onclick={runCompact}>
				{compactBusy ? 'compacting…' : 'Compact now'}
			</button>
			{#if compactResult}<span class="mut">{compactResult}</span>{/if}
		</div>
		{#if gcError}<p class="error">{gcError}</p>{/if}
	</div>
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
	.error {
		color: var(--fail);
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
	.chip {
		background: var(--panel-2);
		border: 1px solid var(--line);
		border-radius: var(--radius-sm);
		padding: 0 7px;
	}
	.chip.off {
		border-color: color-mix(in srgb, var(--amber) 55%, var(--line));
	}
	.policy-edit {
		display: flex;
		flex-wrap: wrap;
		gap: 12px;
		align-items: end;
		font-size: 12px;
		color: var(--mut);
	}
	.policy-edit label {
		display: flex;
		flex-direction: column;
		gap: 3px;
	}
	.policy-edit label.check {
		flex-direction: row;
		align-items: center;
		gap: 6px;
	}
	.policy-edit input[type='number'],
	.gc input[type='number'] {
		width: 110px;
		background: var(--panel-2);
		border: 1px solid var(--line);
		border-radius: var(--radius-sm);
		color: var(--ink);
		padding: 4px 8px;
		font-size: 12px;
	}
	.gc {
		margin-top: 14px;
		padding-top: 12px;
		border-top: 1px solid color-mix(in srgb, var(--line) 45%, transparent);
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
		color: var(--mut);
	}
	.gc-compact {
		align-items: center;
		margin-top: 8px;
	}
	.row {
		display: flex;
		gap: 8px;
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
	.btn.ghost {
		background: none;
		color: var(--mut);
	}
	.btn.danger {
		color: var(--fail);
	}
</style>
