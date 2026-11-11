<script lang="ts">
	// The access-review panel: who holds which can_* action on a catalog object, expanded through the
	// FGA model (roles, nested roles, the parent cascade). `kind` picks the object, a table (the
	// default) or a namespace, and the owner gate is that type's bar (can_drop / can_delete). Owner-only
	// by design: the catalog gates the enumeration, so a non-owner sees the denial, never the ACL.
	// The page hands in its `client`, the catalog calls bound to its own reads and writes, so the panel
	// owns the flow and the page the transport.
	// Collapsed by default: one owner-tier round-trip per object, with definitive outcomes (the ACL,
	// 401/403/501) cached and transient failures (offline, 5xx) retried on the next open.
	import { ChevronRight, ShieldCheck } from '@lucide/svelte';
	import { Select } from '@rask/ui/select';
	import type { ApiResult } from '#lib/api/client.ts';
	import type {
		AccessCheck,
		AccessGrant,
		AccessKind,
		AccessList,
		MyPermissions,
	} from '#lib/data/namespace.ts';
	import { GrantForm } from '../grant-form/index.ts';
	import Subject from '../identity/subject.svelte';

	/** The catalog calls the panel makes: the review, a check, and grant/revoke, all owner-gated by the
	 *  catalog. */
	export type GrantsClient = {
		fetchAccess: (kind: AccessKind, id: string) => Promise<ApiResult<AccessList>>;
		checkAccess: (
			kind: AccessKind,
			id: string,
			user: string,
			relation: string,
		) => Promise<ApiResult<AccessCheck>>;
		grantAccess: (
			kind: AccessKind,
			id: string,
			user: string,
			relation: string,
		) => Promise<ApiResult<AccessGrant>>;
		revokeAccess: (
			kind: AccessKind,
			id: string,
			user: string,
			relation: string,
		) => Promise<ApiResult<AccessGrant>>;
		/** The caller's own verdicts on this object (`{ can_grant_reader: true, … }`), so a refused
		 *  Grant or Revoke renders disabled with its reason. Optional: absent means unknown, which
		 *  renders the controls live, so a verdict is never invented from a read that was not made. */
		fetchMyPermissions?: (kind: AccessKind, id: string) => Promise<ApiResult<MyPermissions>>;
	};

	let {
		dataset,
		kind = 'table',
		client,
	}: { dataset: string; kind?: AccessKind; client: GrantsClient } = $props();

	// #143: the caller's own verdicts, KEYED BY DATASET like `review.for` / `simFor` / `mgFor` above.
	// An unkeyed field would let one object's permissions gate another's buttons on navigation — the
	// exact cross-dataset bleed every other piece of state in this component is keyed to avoid.
	let perms = $state<{ for: string; map: Record<string, boolean> } | null>(null);
	// `null` means UNKNOWN (no `fetchMyPermissions` wired, or the read failed), and GrantForm renders
	// live on unknown: the gate explains a refusal, it does not invent one from a missing read.
	const permMap = $derived(perms?.for === dataset ? perms.map : null);

	// Every piece of state is keyed by the dataset it belongs to, so one dataset's in-flight review
	// never blocks or shows on another's: the panel is open only for the dataset it was opened on, a review/spinner/failure is shown
	// only for the dataset it was produced for — switching datasets blanks them by derivation.
	let openedFor = $state<string | null>(null);
	let review = $state<{
		for: string;
		access: AccessList | null;
		denied: string | null;
	} | null>(null);
	let loadingFor = $state<string | null>(null);
	let failedFor = $state<string | null>(null); // transient failure — never cached, reopen retries

	// #68 access-check simulator — probe an arbitrary (user, relation) against THIS dataset. Owner-gated
	// server-side (the same can_drop bar as the review). Keyed by dataset so a verdict never bleeds across nav.
	let simUser = $state('');
	let simRelation = $state('');
	let simFor = $state<string | null>(null);
	let simVerdict = $state<{ user: string; relation: string; allowed: boolean } | null>(null);
	let simBusy = $state(false);
	let simError = $state<string | null>(null);

	// #72 manage-access form — grant/revoke a base rung to a subject. Keyed by dataset like the simulator.
	let mgUser = $state('');

	const open = $derived(openedFor === dataset);
	const shown = $derived(review?.for === dataset ? review : null);

	// Declared AFTER `shown`, which it reads. `$derived` is lazy so the original order worked at
	// runtime, but TypeScript reported a genuine use-before-declaration and a gate that has to be
	// argued with is a gate that stops being read.

	const loading = $derived(loadingFor === dataset);
	const failed = $derived(failedFor === dataset);

	async function toggle(): Promise<void> {
		if (open) {
			openedFor = null;
			return;
		}
		openedFor = dataset;
		if (shown !== null || loading) return;
		loadingFor = dataset;
		failedFor = null;
		const current = dataset;
		// The caller's own verdicts, fetched ALONGSIDE the review rather than on click. #143 renders a
		// refused control disabled WITH ITS REASON, so the verdict has to be in hand before the user
		// reaches for the button — asking at click time would be the post-hoc 403 this replaces.
		// Fire-and-forget: a failure here must not block the review, which is the panel's main job.
		if (client.fetchMyPermissions) {
			void client.fetchMyPermissions(kind, current).then((r) => {
				if (dataset !== current) return; // latest-wins, same rule as the review below
				perms = r.ok ? { for: current, map: r.data.permissions } : null;
			});
		}
		try {
			const res = await client.fetchAccess(kind, current);
			// Latest-wins: the user clicked away while this was in flight — drop the stale result.
			if (dataset !== current) return;
			if (res.ok) {
				review = { for: current, access: res.data, denied: null };
			} else if (res.status === 401 || res.status === 403 || res.status === 406) {
				const denied =
					res.status === 401
						? 'Sign in to review access.'
						: res.status === 403
							? `Owner access required to review who can reach this ${kind}.`
							: 'This stack runs auth-off — there are no grants to review.';
				review = { for: current, access: null, denied };
			} else {
				failedFor = current; // offline / 5xx: shown but not cached, so the next open retries
			}
		} finally {
			if (loadingFor === current) loadingFor = null;
		}
	}

	// Hide the empty rows: a relation nobody holds is noise in a review of who has access.
	const held = $derived(shown?.access ? shown.access.grants.filter((g) => g.users.length > 0) : []);
	// Every can_* action the model defines on this type (incl. ones nobody holds) — the options you can
	// simulate, taken from the unfiltered grants the review already fetched. Verdict keyed to this dataset.
	const relations = $derived(shown?.access ? shown.access.grants.map((g) => g.relation) : []);
	const simVerdictShown = $derived(simFor === dataset ? simVerdict : null);

	async function runCheck(): Promise<void> {
		const user = simUser.trim();
		if (simBusy || !user || !simRelation) return;
		simBusy = true;
		simError = null;
		const current = dataset;
		try {
			const res = await client.checkAccess(kind, current, user, simRelation);
			if (dataset !== current) return; // navigated away — drop the stale verdict
			if (res.ok) {
				simVerdict = {
					user: res.data.user,
					relation: res.data.relation,
					allowed: res.data.allowed,
				};
				simFor = current;
			} else if (res.status === 401 || res.status === 403) {
				simError = `Simulating access needs the owner tier on this ${kind}.`;
			} else {
				simError = `Check failed (HTTP ${res.status}).`;
			}
		} finally {
			simBusy = false;
		}
	}

	// #72 grant or revoke a base rung, then re-fetch the review so the change is visible immediately.
	// Named rather than inlined into `onmutated=` deliberately: this comment described a handler that
	// had been moved into the markup, so it documented nothing, and `rsvelte-fmt` reformats a
	// multi-statement arrow inside a markup attribute to column 0 — which is what made this file the
	// estate's only `fmt:check` failure.
	async function refreshAfterMutation() {
		const refreshed = await client.fetchAccess(kind, dataset);
		if (refreshed.ok) review = { for: dataset, access: refreshed.data, denied: null };
	}
</script>

<div class="grants">
	<button class="head" onclick={toggle} aria-expanded={open}>
		<span class={['chev', { open }]}><ChevronRight size={12} /></span>
		<ShieldCheck size={12} />
		<span>Access review</span>
	</button>
	{#if open}
		{#if loading}
			<p class="mut">Reviewing access…</p>
		{:else if failed}
			<p class="mut">Access review unavailable right now — close and reopen to retry.</p>
		{:else if shown?.denied}
			<p class="mut">{shown.denied}</p>
		{:else if shown?.access}
			{#if held.length === 0}
				<p class="mut">No user holds any action on this {kind} (grants may target roles only).</p>
			{:else}
				<table class="acl">
					<thead><tr><th>action</th><th>who</th></tr></thead>
					<tbody>
						{#each held as grant (grant.relation)}
							<tr>
								<td class="mono rel">{grant.relation}</td>
								<!-- One flex-gapped chip per holder (#68), so two holders never read as one string. The
								     chip shows the FULL subject with a copy control, because the raw value is what a
								     tuple write or a support ticket needs and no directory resolves it to a name. -->
								<td class="whos">
									{#each grant.users as user (user)}
										{#if user === '*'}
											<span class="who mono wild">everyone (*)</span>
										{:else}
											<Subject
												value={user}
												class="bg-muted border-border mr-1 mb-[3px] rounded-sm border px-1.5"
											/>
										{/if}
									{/each}
								</td>
							</tr>
						{/each}
					</tbody>
				</table>
			{/if}

			<div class="sim">
				<div class="sim-head">Check a specific access</div>
				<div class="sim-form">
					<input
						class="mono"
						placeholder="principal (idp~user id), or role:…#assignee"
						bind:value={simUser}
						onkeydown={(e) => e.key === 'Enter' && runCheck()}
					/>
					<Select
						bind:value={simRelation}
						ariaLabel="Simulate action"
						placeholder="action…"
						options={relations.map((r) => ({ value: r, label: r }))}
					/>
					<button
						class="btn"
						disabled={simBusy || !simUser.trim() || !simRelation}
						onclick={runCheck}
					>
						{simBusy ? '…' : 'Check'}
					</button>
				</div>
				{#if simError}
					<p class="mut">{simError}</p>
				{:else if simVerdictShown}
					<p
						class={['verdict', { allow: simVerdictShown.allowed, deny: !simVerdictShown.allowed }]}
					>
						<Subject value={simVerdictShown.user} />
						{simVerdictShown.allowed ? 'can' : 'cannot'}
						<span class="mono">{simVerdictShown.relation}</span> on this {kind}.
					</p>
				{/if}
			</div>

			<div class="sim">
				<div class="sim-head">Manage access (grant / revoke)</div>
				<!-- The grant form the access graph renders too. `knownSubjects` comes from the review this
				     panel already renders, the only subject directory there is. -->
				<GrantForm
					{kind}
					bind:subject={mgUser}
					permissions={permMap}
					knownSubjects={(shown?.access?.grants ?? []).flatMap((g) => g.users)}
					grant={(user, relation) => client.grantAccess(kind, dataset, user, relation)}
					revoke={(user, relation) => client.revokeAccess(kind, dataset, user, relation)}
					onmutated={refreshAfterMutation}
				/>
			</div>
		{/if}
	{/if}
</div>

<style>
	.grants {
		display: flex;
		flex-direction: column;
		gap: 6px;
		margin: 2px 0 10px;
	}
	.head {
		display: inline-flex;
		align-items: center;
		gap: 5px;
		background: none;
		border: none;
		padding: 0;
		color: var(--mut);
		font: inherit;
		font-size: 12px;
		cursor: pointer;
	}
	.head:hover {
		color: var(--ink);
	}
	.chev {
		display: inline-flex;
		transition: transform 0.12s ease;
	}
	.chev.open {
		transform: rotate(90deg);
	}
	.mut {
		color: var(--faint);
		font-size: 12px;
		margin: 0;
	}
	.acl {
		border-collapse: collapse;
		font-size: 12px;
	}
	.acl th {
		text-align: left;
		color: var(--faint);
		font-weight: 500;
		padding: 2px 14px 2px 0;
	}
	.acl td {
		padding: 2px 14px 2px 0;
		vertical-align: top;
	}
	.rel {
		color: var(--mut);
		white-space: nowrap;
	}
	.whos {
		display: flex;
		flex-wrap: wrap;
		gap: 4px;
		align-items: center;
	}
	.who {
		display: inline-block;
		background: var(--panel-2);
		border: 1px solid var(--line);
		border-radius: var(--radius-sm);
		padding: 0 6px;
		margin: 0 4px 3px 0;
	}
	.who.wild {
		border-color: color-mix(in srgb, var(--amber) 55%, var(--line));
	}
	.sim {
		margin-top: 10px;
		padding-top: 8px;
		border-top: 1px solid color-mix(in srgb, var(--line) 55%, transparent);
	}
	.sim-head {
		color: var(--faint);
		font-size: 11px;
		text-transform: uppercase;
		letter-spacing: 0.05em;
		margin-bottom: 5px;
	}
	.sim-form {
		display: flex;
		flex-wrap: wrap;
		gap: 6px;
		align-items: center;
	}
	.sim-form input {
		background: var(--panel-2);
		border: 1px solid var(--line);
		border-radius: var(--radius-sm);
		color: var(--ink);
		font-size: 12px;
		padding: 3px 7px;
	}
	.sim-form input {
		flex: 1 1 220px;
		min-width: 160px;
	}
	.btn {
		background: var(--panel-2);
		border: 1px solid var(--line);
		border-radius: var(--radius-sm);
		color: var(--ink);
		font-size: 12px;
		padding: 3px 12px;
		cursor: pointer;
	}
	.btn:disabled {
		opacity: 0.5;
		cursor: default;
	}
	.verdict {
		font-size: 12px;
		margin: 8px 0 0;
	}
	.verdict.allow {
		color: var(--ok);
	}
	.verdict.deny {
		color: var(--fail);
	}
</style>
