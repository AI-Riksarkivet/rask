<script lang="ts">
	// #65 a project's maintenance plane: the project's own policy (the sweep's fallback for every
	// dataset no lower record claims) with set/edit/remove, and a read-only view of every record that
	// SHADOWS it. ONE read answers all three levels: the project's own record plus every table and
	// namespace record inside it. The page holds that read and keys this section by the project, so a
	// draft opened on A never survives into B, where Save would write A's knobs onto B's tenant.
	//
	// `unavailable` is the load-bearing state: a failed read must never render as "no policy", because
	// that reads as an invitation to Save, which would overwrite a live record from a draft built on
	// nothing.
	import { Trash2 } from '@lucide/svelte';
	import { Button } from '@riksarkivet/ui/button';
	import { resolve } from '$app/paths';
	import PolicyFields from '#lib/data/PolicyFields.svelte';
	import { policyRequestFrom, type PolicyDraft } from '#lib/policies.ts';
	import {
		deleteProjectPolicy,
		fetchProjectPolicies,
		setProjectPolicy,
	} from '#lib/remote/policies.api.ts';

	let { project, read }: { project: string; read: ReturnType<typeof fetchProjectPolicies> } =
		$props();

	const answer = $derived(read.current);
	const policies = $derived(answer?.ok ? answer.data : null);
	const part = $derived.by((): 'loading' | 'ok' | 'denied' | 'unavailable' => {
		if (read.error !== undefined) return 'unavailable';
		if (answer === undefined) return 'loading';
		if (answer.ok) return 'ok';
		if (answer.status === 401 || answer.status === 403) return 'denied';
		return 'unavailable';
	});
	const denied = $derived(
		answer?.ok === false && answer.status === 401
			? 'Sign in to view this project’s maintenance policies.'
			: 'Denied: maintenance policies need project-admin access.',
	);

	/** The project's OWN record, if it has one — the one this page can edit. */
	const ownPolicy = $derived(
		policies?.policies.find((p) => p.kind === 'project' && p.id === project) ?? null,
	);
	/** Everything below it, which SHADOWS it dataset-by-dataset. Read-only here: each belongs to the
	 *  lakehouse rung that owns it, and every row links there. */
	const scopedPolicies = $derived((policies?.policies ?? []).filter((p) => p.kind !== 'project'));

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
		// Seeded from the STORED record, so an untouched field round-trips its own value rather than
		// the model's default (`_record` merges on `exclude_unset`, but a form that echoed defaults back
		// would be SETTING them).
		draft = {
			retention_days: ownPolicy?.retention_days ?? null,
			retain_versions: ownPolicy?.retain_versions ?? null,
			interval: ownPolicy?.compact_interval_hours ?? null,
			target: ownPolicy?.target_rows_per_fragment ?? null,
			enabled: ownPolicy?.compact_enabled ?? true,
		};
		writeError = null;
		editing = true;
	}

	function fail(status: number, detailText: string): void {
		if (status === 401) writeError = 'Sign in to edit the maintenance policy.';
		else if (status === 403)
			writeError = 'Denied: a project policy needs project-admin access (can_administer).';
		else writeError = detailText;
	}

	/** The write refreshes the listing in its own flight; the section waits for that answer. */
	async function settled(): Promise<void> {
		await fetchProjectPolicies(project);
	}

	async function save(): Promise<void> {
		if (busy) return;
		busy = true;
		writeError = null;
		try {
			const res = await setProjectPolicy({ project, policy: policyRequestFrom(draft) });
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
			const res = await deleteProjectPolicy(project);
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
	<h2>Maintenance</h2>
	<p class="mut">
		A project policy is the sweep's fallback for this tenant's data. Resolution is
		<strong>winner-takes-all</strong>: a table record shadows a namespace record shadows this one,
		and the winning record supplies <em>every</em> field — a value it leaves unset falls to the
		sweep's global default, not to the record it shadowed. Tag-pinned versions (e.g.
		<code class="mono">blessed</code>) are never cleaned up, whatever any policy says.
	</p>

	{#if part === 'loading'}
		<p class="mut">Loading policies…</p>
	{:else if part === 'denied'}
		<p class="mut">{denied}</p>
	{:else if part === 'unavailable'}
		<p class="mut">
			Policies unavailable right now — not shown, to avoid an overwriting edit against a stale read.
		</p>
	{:else if policies}
		{#if editing}
			<PolicyFields bind:draft {busy} onsave={save} oncancel={() => (editing = false)}>
				<p class="mut">
					An empty field means <em>inherit</em> — it is left out of the request, not sent as a cleared
					value, so the knobs this form has no control for (the compaction scan batch, auto-cleanup ownership)
					keep whatever the record already carries.
				</p>
			</PolicyFields>
		{:else if ownPolicy}
			<div class="refs">
				{#if ownPolicy.retention_days}<span class="chip mono"
						>retention {ownPolicy.retention_days}d</span
					>{/if}
				{#if ownPolicy.retain_versions}<span class="chip mono"
						>keep last {ownPolicy.retain_versions}</span
					>{/if}
				{#if ownPolicy.compact_interval_hours}<span class="chip mono"
						>every {ownPolicy.compact_interval_hours}h</span
					>{/if}
				{#if ownPolicy.target_rows_per_fragment}<span class="chip mono">target {ownPolicy.target_rows_per_fragment}
				rows/frag</span>{/if}
				{#if !ownPolicy.compact_enabled}<span class="chip off mono">maintenance off</span>{/if}
				<Button variant="ghost" size="sm" onclick={startEdit}>Edit</Button>
				<Button variant="destructive" size="sm" disabled={busy} onclick={remove}>
					<Trash2 size={12} /> Remove
				</Button>
			</div>
			{#if ownPolicy.buckets && ownPolicy.buckets.length > 0}
				<p class="mut">
					Covers <span class="mono">{ownPolicy.buckets.join(
						', ',
					)}</span> — the warehouse buckets resolved
					when the policy was set. A warehouse provisioned since is not covered until the policy is set
					again.
				</p>
			{/if}
		{:else}
			<p class="mut">
				No project policy — the sweep applies its global defaults to anything no table or namespace
				record claims.
				<Button variant="ghost" size="sm" class="ml-1.5" onclick={startEdit}>Set policy</Button>
			</p>
		{/if}
		{#if shownError}<p class="error">{shownError}</p>{/if}

		<h3>Records inside this project</h3>
		{#if scopedPolicies.length === 0}
			<p class="mut">
				No table or namespace policy in this project{policies.warehouses.length === 0
					? ' (it has no warehouses yet)'
					: ''}.
			</p>
		{:else}
			<table>
				<thead><tr><th>kind</th><th>object</th><th>retention</th><th>state</th></tr></thead>
				<tbody>
					{#each scopedPolicies as p (`${p.kind}:${p.id}`)}
						<tr>
							<td class="mono">{p.kind}</td>
							<td>
								<!-- DOWN a rung: the namespace's or table's own catalog page. -->
								<a
									class="mono whlink"
									href={p.kind === 'table'
										? resolve('/catalog/tables/[table]', { table: encodeURIComponent(p.id) })
										: resolve('/catalog/namespaces/[id]', { id: encodeURIComponent(p.id) })}
									>{p.id}</a
								>
							</td>
							<td class="mono">{p.retention_days ? `${p.retention_days}d` : '—'}</td>
							<td>
								<span class="chip mono" class:off={!p.compact_enabled}
									>{p.compact_enabled ? 'on' : 'off'}</span
								>
							</td>
						</tr>
					{/each}
				</tbody>
			</table>
		{/if}
	{/if}
</section>

<style>
	section {
		margin-bottom: 26px;
	}
	h2 {
		font-size: 14px;
		margin: 0 0 8px;
	}
	h3 {
		font-size: 12px;
		margin: 18px 0 6px;
		color: var(--muted-foreground);
	}
	.mut {
		color: var(--muted-foreground);
		font-size: 12px;
		margin: 4px 0;
	}
	.error {
		font-size: 12px;
		color: var(--destructive);
		margin: 6px 0 0;
	}
	table {
		border-collapse: collapse;
		font-size: 12px;
		width: 100%;
	}
	th {
		text-align: left;
		color: var(--muted-foreground);
		font-weight: 500;
		padding: 3px 14px 3px 0;
		border-bottom: 1px solid var(--border);
	}
	td {
		padding: 5px 14px 5px 0;
		border-bottom: 1px solid color-mix(in srgb, var(--border) 45%, transparent);
	}
	.whlink {
		color: var(--foreground);
		text-decoration: none;
	}
	.whlink:hover {
		text-decoration: underline;
	}
	.chip {
		background: var(--muted);
		border: 1px solid color-mix(in srgb, var(--success) 45%, var(--border));
		border-radius: var(--radius-sm);
		padding: 0 7px;
	}
	.chip.off {
		border-color: color-mix(in srgb, var(--warning) 55%, var(--border));
	}
	.refs {
		display: flex;
		flex-wrap: wrap;
		gap: 6px;
	}
</style>
