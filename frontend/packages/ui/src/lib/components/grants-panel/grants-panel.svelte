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
	import { Select } from '../select/index.js';

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
	};

	let {
		dataset,
		kind = 'table',
		client,
	}: { dataset: string; kind?: GrantsKind; client: GrantsClient } = $props();

	// #72 the base rungs an owner may hand out (owner/writer/reader/validator) — the model's directly
	// assignable relations, least→most privilege. NOT the can_* actions the review row shows (those are
	// derived); a grant/revoke writes/deletes a direct base-rung tuple, then the review is re-fetched.
	const GRANTABLE = ['reader', 'writer', 'validator', 'owner'];

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
	let mgRelation = $state('');
	let mgBusy = $state(false);
	let mgFor = $state<string | null>(null);
	let mgResult = $state<{ tone: 'ok' | 'fail'; text: string } | null>(null);

	const open = $derived(openedFor === dataset);
	const shown = $derived(review?.for === dataset ? review : null);
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
	async function runManage(grant: boolean): Promise<void> {
		const user = mgUser.trim();
		if (mgBusy || !user || !mgRelation) return;
		mgBusy = true;
		mgResult = null;
		const current = dataset;
		try {
			const res = grant
				? await client.grantAccess(kind, current, user, mgRelation)
				: await client.revokeAccess(kind, current, user, mgRelation);
			if (dataset !== current) return; // navigated away — drop the stale result
			mgFor = current;
			if (res.ok) {
				const verb = grant ? 'granted to' : 'revoked from';
				mgResult = { tone: 'ok', text: `${mgRelation} ${verb} ${res.data.user}.` };
				const refreshed = await client.fetchAccess(kind, current);
				if (dataset === current && refreshed.ok) {
					review = { for: current, access: refreshed.data, denied: null };
				}
			} else if (res.status === 401 || res.status === 403) {
				mgResult = { tone: 'fail', text: `Managing access needs the owner tier on this ${kind}.` };
			} else if (res.status === 400 || res.status === 422) {
				mgResult = { tone: 'fail', text: `${mgRelation} is not a grantable rung here.` };
			} else {
				mgResult = { tone: 'fail', text: `Failed (HTTP ${res.status}).` };
			}
		} finally {
			mgBusy = false;
		}
	}

	const mgResultShown = $derived(mgFor === dataset ? mgResult : null);
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
								<td>
									{#each grant.users as user (user)}
										<span class="who mono" class:wild={user === '*'}
											>{user === '*' ? 'everyone (*)' : user}</span
										>
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
						placeholder="user (e.g. alice), or role:… / team:…#member"
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
				<div class="sim-form">
					<input
						class="mono"
						placeholder="user (e.g. alice), or role:… / team:…#member"
						bind:value={mgUser}
					/>
					<Select
						bind:value={mgRelation}
						ariaLabel="Grant rung"
						placeholder="rung…"
						options={GRANTABLE.map((r) => ({ value: r, label: r }))}
					/>
					<button
						class="btn"
						disabled={mgBusy || !mgUser.trim() || !mgRelation}
						onclick={() => runManage(true)}
					>
						{mgBusy ? '…' : 'Grant'}
					</button>
					<button
						class="btn ghost"
						disabled={mgBusy || !mgUser.trim() || !mgRelation}
						onclick={() => runManage(false)}
					>
						Revoke
					</button>
				</div>
				{#if mgResultShown}
					<p
						class="verdict"
						class:allow={mgResultShown.tone === 'ok'}
						class:deny={mgResultShown.tone === 'fail'}
					>
						{mgResultShown.text}
					</p>
				{/if}
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
	.btn.ghost {
		background: none;
		color: var(--mut);
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
