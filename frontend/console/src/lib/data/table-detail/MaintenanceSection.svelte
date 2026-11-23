<script lang="ts">
	// The maintenance plane: the #50 policy (owner-gated set/delete), and the #75 on-demand GC
	// (preview → two-click destructive reclaim) in `./GarbageCollection.svelte`. The catalog compacts nothing — compaction and index
	// rebuilds are bring-your-own compute, which the policy's compaction fields steer through the
	// maintenance sweep's due list.
	//
	// Split out of TableDetail.svelte (#98). Mounted under the parent's `{#key table}`, so the
	// instance never survives a navigation — an open editor or an armed GC confirm dies with it.
	import { Trash2 } from '@lucide/svelte';
	import { Button } from '@riksarkivet/ui/button';
	import { Checkbox } from '@riksarkivet/ui/checkbox';
	import { Input } from '@riksarkivet/ui/input';
	import type { Policy } from '#lib/policies.ts';
	import { policyRequestFrom } from '../namespace';
	import { deleteTablePolicy, setTablePolicy } from '../remote/catalog.api';
	import GarbageCollection from './GarbageCollection.svelte';

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

	/** A number field in the policy form: a fixed width, dense. */
	const NUMBER = 'h-7 w-28 font-mono text-xs';

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
		else if (status === 403)
			policyError = 'Denied: policy changes need the modifier rung (can_drop).';
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
</script>

<section>
	<h2>Maintenance policy</h2>
	{#if editingPolicy}
		<div class="policy-edit">
			<label
				>retention days <Input
					class={NUMBER}
					type="number"
					min="1"
					bind:value={draft.retention_days}
					placeholder="global default"
				/></label
			>
			<label
				>retain versions <Input
					class={NUMBER}
					type="number"
					min="1"
					bind:value={draft.retain_versions}
					placeholder="—"
				/></label
			>
			<label
				>compact every (h) <Input
					class={NUMBER}
					type="number"
					min="1"
					bind:value={draft.interval}
					placeholder="every sweep"
				/></label
			>
			<label
				>target rows/fragment <Input
					class={NUMBER}
					type="number"
					min="1024"
					bind:value={draft.target}
					placeholder="Lance default"
				/></label
			>
			<label class="check"><Checkbox bind:checked={draft.enabled} /> maintenance enabled</label>
			<div class="row">
				<Button size="sm" disabled={busy} onclick={savePolicy}>Save policy</Button>
				<Button variant="ghost" size="sm" onclick={() => (editingPolicy = false)}>Cancel</Button>
			</div>
		</div>
	{:else if policyUnavailable}
		<p class="mut">
			Policy unavailable right now — not shown to avoid an overwriting edit against a stale read.
		</p>
	{:else if policy}
		<div class="refs">
			{#if policy.retention_days}<span class="chip mono">retention {policy.retention_days}d</span
				>{/if}
			{#if policy.retain_versions}<span class="chip mono">keep last {policy.retain_versions}</span
				>{/if}
			{#if policy.compact_interval_hours}<span class="chip mono"
					>every {policy.compact_interval_hours}h</span
				>{/if}
			{#if policy.target_rows_per_fragment}<span class="chip mono">target {policy.target_rows_per_fragment}
			rows/frag</span>{/if}
			{#if !policy.compact_enabled}<span class="chip off mono">maintenance off</span>{/if}
			<Button variant="ghost" size="sm" onclick={startPolicyEdit}>Edit</Button>
			<Button variant="destructive" size="sm" disabled={busy} onclick={removePolicy}>
				<Trash2 size={12} /> Remove
			</Button>
		</div>
		<p class="mut">
			Enforced by the maintenance sweep; tag-pinned versions (e.g. blessed) are never cleaned up.
		</p>
	{:else}
		<p class="mut">
			No policy — the sweep applies the global defaults.
			<Button variant="ghost" size="sm" onclick={startPolicyEdit}>Set policy</Button>
		</p>
	{/if}
	{#if policyError}<p class="error">{policyError}</p>{/if}

	<!-- #75 on-demand GC: dry-run reclaimable versions, then reclaim (owner-gated, destructive). -->
	<GarbageCollection {table} {onchanged} />
</section>

<style>
	section {
		margin-bottom: 22px;
	}
	h2 {
		font-size: 13px;
		text-transform: uppercase;
		letter-spacing: 0.06em;
		color: var(--muted-foreground);
		margin: 0 0 8px;
	}
	.mut {
		color: var(--muted-foreground);
		font-size: 12px;
	}
	.error {
		color: var(--destructive);
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
		background: var(--muted);
		border: 1px solid var(--border);
		border-radius: var(--radius-sm);
		padding: 0 7px;
	}
	.chip.off {
		border-color: color-mix(in srgb, var(--warning) 55%, var(--border));
	}
	.policy-edit {
		display: flex;
		flex-wrap: wrap;
		gap: 12px;
		align-items: end;
		font-size: 12px;
		color: var(--muted-foreground);
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
	.row {
		display: flex;
		gap: 8px;
	}
</style>
