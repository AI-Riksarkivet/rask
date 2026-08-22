<script lang="ts">
	// The access-review panel (#51): who holds which can_* action on a catalog object, expanded through
	// the FGA model (roles, teams, the parent cascade). Kind-generalized (sweep group 3): `kind` picks
	// the catalog surface — 'table' (the default) or 'namespace' — and the owner gate is the per-type
	// bar (can_drop / can_delete). Owner-only by design — the catalog gates the enumeration, so a
	// non-owner sees the denial state, never the ACL. Hoisted into @rask/ui (audit 2026-07-24: the data
	// and lineage copies had silently drifted apart). Transport-agnostic (rask convention, the
	// StatusBoard precedent: the lib never owns an API client) — the zone injects its own `client`,
	// and the Grants* types are the STRUCTURAL shapes this panel reads, so any zone whose generated
	// catalog types carry these fields can pass its functions straight through without adapters.
	// Collapsed by default: one owner-tier round-trip per dataset, with definitive outcomes (the ACL,
	// 401/403/501) cached and transient failures (offline, 5xx) retried on the next open.
	import { ChevronRight, ShieldCheck } from '@lucide/svelte';
	import { GrantForm } from '../grant-form/index.js';
	import { Select } from '../select/index.js';
	import Subject from '../identity/subject.svelte';

	export type GrantsKind = 'table' | 'namespace';
	/** Mirrors the zones' status-aware ApiResult (http.ts): 0 = fetch-level failure/offline. */
	export type GrantsResult<T> =
		| { ok: true; data: T }
		| { ok: false; status: number; detail: string };
	export type GrantsAccessList = { grants: { relation: string; users: string[] }[] };
	/** The zone-owned catalog seam: review + #68 check + #72 grant/revoke, all owner-gated SERVER-side. */
	export type GrantsClient = {
		fetchAccess: (kind: GrantsKind, id: string) => Promise<GrantsResult<GrantsAccessList>>;
		checkAccess: (
			kind: GrantsKind,
			id: string,
			user: string,
			relation: string,
		) => Promise<GrantsResult<{ user: string; relation: string; allowed: boolean }>>;
		grantAccess: (
			kind: GrantsKind,
			id: string,
			user: string,
			relation: string,
		) => Promise<GrantsResult<{ user: string }>>;
		revokeAccess: (
			kind: GrantsKind,
			id: string,
			user: string,
			relation: string,
		) => Promise<GrantsResult<{ user: string }>>;
		/** The caller's OWN verdicts on this object — `{ can_grant_reader: true, … }`.
		 *
		 *  OPTIONAL on purpose: two of the three call sites do not fetch it yet, and a required member
		 *  would break them at the type level. Absent means "unknown", which renders the controls live
		 *  exactly as before — this makes the gate possible, it does not make it silently fail closed on
		 *  a caller that has not been wired up.
		 *
		 *  A 5th client member rather than a `permissions` prop, because the panel owns the flow and the
		 *  zone owns the transport (the rule this whole file follows). A prop would push per-dataset
		 *  refetching into every caller. */
		fetchMyPermissions?: (
			kind: GrantsKind,
			id: string,
		) => Promise<GrantsResult<{ permissions: Record<string, boolean> }>>;
	};

	let {
		dataset,
		kind = 'table',
		client,
	}: { dataset: string; kind?: GrantsKind; client: GrantsClient } = $props();

	// #143: the caller's own verdicts, KEYED BY DATASET like `review.for` / `simFor` / `mgFor` above.
	// An unkeyed field would let one object's permissions gate another's buttons on navigation — the
	// exact cross-dataset bleed every other piece of state in this component is keyed to avoid.
	let perms = $state<{ for: string; map: Record<string, boolean> } | null>(null);
	// Keyed by dataset, like every other field here — an unkeyed map would let one object's verdicts
	// gate another's buttons after a navigation, the cross-dataset bleed the 2026-07-16 audit fixed.
	// `null` means UNKNOWN (no `fetchMyPermissions` wired, or the read failed), and GrantForm renders
	// live on unknown: the gate explains a refusal, it does not invent one from a missing read.
	const permMap = $derived(perms?.for === dataset ? perms.map : null);

	// Every piece of state is keyed by the dataset it belongs to (no cross-dataset bleed, audit
	// 2026-07-16: a single un-keyed `loading` let one dataset's in-flight review block another's):
	// the panel is open only for the dataset it was opened on, a review/spinner/failure is shown
	// only for the dataset it was produced for — switching datasets blanks them by derivation.
	let openedFor = $state<string | null>(null);
	let review = $state<{
		for: string;
		access: GrantsAccessList | null;
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
			} else if (res.status === 401 || res.status === 403 || res.status === 501) {
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
		<span class="chev" class:open><ChevronRight size={12} /></span>
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
								<!-- One flex-gapped chip per holder (#68): adjacent inline spans rendered two
								     subjects as ONE glued string once an opaque OIDC sub wrapped. The chip shows
								     the display form; the FULL subject rides `title`, because the raw value is
								     what a tuple write or a support ticket needs. -->
								<td class="whos">
									{#each grant.users as user (user)}
										{#if user === '*'}
											<span class="who mono wild">everyone (*)</span>
										{:else}
											<Subject value={user} class="who" />
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
						placeholder="OIDC sub, or role:…#assignee / team:…#member"
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
					<p class="verdict" class:allow={simVerdictShown.allowed} class:deny={!simVerdictShown.allowed}>
						<span class="mono">{simVerdictShown.user}</span>
						{simVerdictShown.allowed ? 'can' : 'cannot'}
						<span class="mono">{simVerdictShown.relation}</span> on this {kind}.
					</p>
				{/if}
			</div>

			<div class="sim">
				<div class="sim-head">Manage access (grant / revoke)</div>
				<!-- ONE implementation, shared with the lakehouse's AccessGraph. It lived in both, and on
				     2026-08-16 three defects each had to be fixed twice while the copies had already
				     drifted where nobody was looking (four rungs there, six here). `knownSubjects` comes
				     from the review this panel already renders — the only subject directory that exists. -->
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
