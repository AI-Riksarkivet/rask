<script lang="ts">
	// A namespace's maintenance policy (#50): the described policy as chips, and set/edit/remove for
	// whoever holds the rung the catalog gates them on. The page holds the read (it is issued once the
	// namespace is known to be there) and keys this section by the namespace, so an editor opened on A
	// never survives into B, where Save would write A's draft.
	//
	// The states: 'none' (the catalog answers `set: false`: global defaults apply), 'set', 'denied'
	// (401/403) and 'unavailable' (a 404, a 5xx, offline or contract drift), which is never an
	// affirmative empty state that would invite an overwriting write.
	import { Trash2 } from '@lucide/svelte';
	import { Button } from '@riksarkivet/ui/button';
	import { GatedAction } from '#lib/access/gated-action/index.ts';
	import type { PolicyDraft } from '#lib/policies.ts';
	import { policyRequestFrom } from './namespace';
	import {
		deleteNamespacePolicy,
		fetchNamespacePolicy,
		setNamespacePolicy,
	} from './remote/namespace.api';
	import PolicyFields from './PolicyFields.svelte';

	let {
		ns,
		read,
		perms,
	}: {
		ns: string;
		read: ReturnType<typeof fetchNamespacePolicy>;
		/** The caller's own verdicts on the namespace; `null` is unanswered, which is not denied. */
		perms: Record<string, boolean> | null;
	} = $props();

	const answer = $derived(read.current);
	const policy = $derived(answer?.ok ? answer.data : null);
	const part = $derived.by((): 'loading' | 'none' | 'set' | 'denied' | 'unavailable' => {
		if (read.error !== undefined) return 'unavailable';
		if (answer === undefined) return 'loading';
		if (answer.ok) return answer.data === null ? 'none' : 'set';
		if (answer.status === 401 || answer.status === 403) return 'denied';
		return 'unavailable';
	});
	const denied = $derived(
		answer?.ok === false && answer.status === 401
			? 'Sign in to view the maintenance policy.'
			: 'Denied: viewing this policy needs a reader rung on the namespace.',
	);

	// Policy set/delete is gated by the catalog on `can_delete` (the owner suffix map in its
	// `authz/rungs.py`), so that is the rung the controls agree with.
	const mayEdit = $derived(perms?.can_delete === true);
	const permsSettled = $derived(perms !== null);
	/** Until the self-view answers the controls render live: "not answered yet" is not "denied". */
	const allowed = $derived(!permsSettled || mayEdit);
	// ONE string, shared by the three gated controls and the sentence below them.
	const REASON_EDIT = $derived(
		`Changing this policy needs the modifier rung (can_delete) on ${ns}.`,
	);

	let editing = $state(false);
	let busy = $state(false);
	let writeError = $state<string | null>(null);
	let draft = $state<PolicyDraft>({
		retention_days: null,
		retain_versions: null,
		interval: null,
		target: null,
		enabled: true,
	});
	/** A read that threw is a contract drift: said, never rendered from. */
	const shownError = $derived(
		writeError ??
			(read.error === undefined
				? null
				: `policy response drifted from the contract: ${String(read.error)}`),
	);

	function startEdit(): void {
		draft = {
			retention_days: policy?.retention_days ?? null,
			retain_versions: policy?.retain_versions ?? null,
			interval: policy?.compact_interval_hours ?? null,
			target: policy?.target_rows_per_fragment ?? null,
			enabled: policy?.compact_enabled ?? true,
		};
		writeError = null;
		editing = true;
	}

	function fail(status: number, detailText: string): void {
		if (status === 401) writeError = 'Sign in to edit the maintenance policy.';
		else if (status === 403)
			writeError = 'Denied: policy changes need the modifier rung (can_delete).';
		else writeError = detailText;
	}

	/** The write refreshes the describe in its own flight; the card waits for that answer, so it
	 *  re-renders from the catalog's post-write state. */
	async function settled(): Promise<void> {
		await fetchNamespacePolicy({ namespace: ns });
	}

	async function save(): Promise<void> {
		if (busy) return;
		busy = true;
		writeError = null;
		try {
			const res = await setNamespacePolicy({ namespace: ns, policy: policyRequestFrom(draft) });
			if (res.ok) {
				editing = false;
				await settled();
			} else fail(res.status, res.detail);
		} catch (err) {
			writeError = `policy response drifted from the contract: ${String(err)}`;
		} finally {
			busy = false;
		}
	}

	async function remove(): Promise<void> {
		if (busy) return;
		busy = true;
		writeError = null;
		try {
			const res = await deleteNamespacePolicy({ namespace: ns });
			if (res.ok) await settled();
			else fail(res.status, res.detail);
		} catch (err) {
			writeError = `policy response drifted from the contract: ${String(err)}`;
		} finally {
			busy = false;
		}
	}
</script>

<section>
	<h2>Maintenance policy</h2>
	<p class="mut">
		A namespace policy governs every dataset under <span class="mono">{ns}</span> unless a table policy
		overrides it; tag-pinned versions (e.g. blessed) are never cleaned up.
	</p>
	{#if editing}
		<PolicyFields bind:draft {busy} onsave={save} oncancel={() => (editing = false)} />
	{:else if part === 'loading'}
		<p class="mut">Loading policy…</p>
	{:else if part === 'denied'}
		<p class="mut">{denied}</p>
	{:else if part === 'unavailable'}
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
			<!-- #143 (owner ruling, 2026-08-16): a gated action stays VISIBLE and disabled with its reason,
			     and the sentence below still names the rung without a hover. `disabled` is conditional on
			     the verdict because GatedAction deliberately avoids the native attribute: it would kill
			     the tooltip and drop the control from the tab order. -->
			<GatedAction {allowed} action="Edit policy" reason={REASON_EDIT}>
				<Button variant="ghost" size="sm" onclick={startEdit}>Edit</Button>
			</GatedAction>
			<GatedAction {allowed} action="Remove policy" reason={REASON_EDIT}>
				<Button variant="destructive" size="sm" disabled={allowed && busy} onclick={remove}>
					<Trash2 size={12} /> Remove
				</Button>
			</GatedAction>
		</div>
	{:else}
		<p class="mut">
			No policy — the sweep applies the global defaults.
			<GatedAction {allowed} action="Set policy" reason={REASON_EDIT}>
				<Button variant="ghost" size="sm" onclick={startEdit}>Set policy</Button>
			</GatedAction>
		</p>
	{/if}
	<!-- Say WHY the controls are refused once the self-view has answered: silence would read as a broken
	     page to someone who expected to edit, and the rung names what to ask for. -->
	{#if permsSettled && !mayEdit}
		<p class="mut">{REASON_EDIT}</p>
	{/if}
	{#if shownError}<p class="error">{shownError}</p>{/if}
</section>

<style>
	section {
		margin-bottom: 26px;
	}
	h2 {
		font-size: 14px;
		margin: 0 0 8px;
	}
	.mut {
		color: var(--muted-foreground);
		font-size: 12px;
		margin: 4px 0;
	}
	.error {
		color: var(--destructive);
		font-size: 12px;
		margin: 6px 0 0;
	}
	.refs {
		display: flex;
		flex-wrap: wrap;
		align-items: center;
		gap: 6px;
	}
	.chip {
		background: var(--muted);
		border: 1px solid var(--border);
		border-radius: var(--radius-sm);
		color: var(--muted-foreground);
		font-size: 12px;
		padding: 1px 8px;
	}
	.chip.off {
		border-color: color-mix(in srgb, var(--warning) 55%, var(--border));
	}
</style>
