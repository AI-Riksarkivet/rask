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
	import { GatedAction } from '../gated-action/index.js';
	import { ChevronRight, ShieldCheck } from '@lucide/svelte';
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

	// #72 the base rungs a grant-manager may hand out — the model's directly assignable relations,
	// least→most privilege. NOT the can_* actions the review row shows (those are derived); a
	// grant/revoke writes/deletes a direct base-rung tuple, then the review is re-fetched.
	//
	// The last two are the GRANT AXIS, and they are not more of the same: the four above say what a
	// subject may do with the DATA, these say what they may do with ACCESS. `manage_grants` hands out
	// any rung here without conferring a single byte of read — the security-admin persona that was
	// unexpressible while granting was welded to `owner`. `pass_grants` is the grant OPTION: it lets a
	// holder hand on what they already hold, and nothing more, so it is worthless on its own.
	//
	// Kept in privilege order rather than grouped, because that is the order the picker reads in and
	// `manage_grants` genuinely outranks `owner` on the axis it belongs to. The catalog gates each one
	// separately (`can_grant_<rung>`), so a rung offered here that the caller may not grant is refused
	// server-side — the picker is a convenience, never the boundary.
	const GRANTABLE = ['reader', 'writer', 'validator', 'owner', 'pass_grants', 'manage_grants'];

	// `reader` and `owner` say what they mean; `pass_grants` and `manage_grants` do not, and picking
	// the wrong one of those hands out the authority to hand out authority. The wire value stays the
	// relation name — this is the label only, so nothing downstream has to know about it.
	const RUNG_LABEL: Record<string, string> = {
		pass_grants: 'pass_grants — may re-grant what they hold',
		manage_grants: 'manage_grants — may grant anything, reads nothing',
	};

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

	// #143 verdicts. `undefined` (no `fetchMyPermissions` wired, or the read failed) means UNKNOWN, and
	// unknown renders the control live — this gate exists to explain a refusal, not to invent one from
	// a missing read. Grant is per-RUNG (`can_grant_<rung>`), revoke is `can_revoke_grant`; they differ
	// and conflating them is the bug this same file carried in its failure message until today.
	const permMap = $derived(perms?.for === dataset ? perms.map : null);
	const mayGrant = $derived(
		permMap === null || !mgRelation ? true : permMap[`can_grant_${mgRelation}`] === true,
	);
	const mayRevoke = $derived(permMap === null ? true : permMap.can_revoke_grant === true);
	const grantReason = $derived(
		`Granting ${mgRelation || 'a rung'} here needs can_grant_${mgRelation || '<rung>'} on this ${kind} — held by a grant-manager, or by someone holding ${mgRelation || 'that rung'} plus the grant option. Owning the ${kind} is not sufficient if access is centrally managed.`,
	);
	const revokeReason = $derived(
		`Revoking here needs can_revoke_grant on this ${kind} — grant-manager only, deliberately stricter than granting so a delegate cannot strip the owner who delegated to them.`,
	);

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
				// NAME THE REAL GATE. This used to read "Managing access needs the owner tier", which was
				// true of `access/list` and `access/check` (both still `can_drop`) and has NOT been true of
				// grant/revoke since the grant axis was separated from ownership: `_authorize_grant` reads
				// the rung out of the BODY, so grant is `can_grant_<rung>` and revoke is `can_revoke_grant`
				// (= `manage_grants` alone). Telling a refused caller to obtain the owner tier is worse than
				// saying nothing — under `managed_access` they may already HOLD it and still be refused,
				// because that flag withdraws exactly `manage_grants` and the grant option beneath it.
				mgResult = {
					tone: 'fail',
					text: grant
						? `Granting ${mgRelation} here needs can_grant_${mgRelation} on this ${kind} — held by a grant-manager, or by someone holding ${mgRelation} plus the grant option. Owning the ${kind} is not sufficient if access is centrally managed.`
						: `Revoking here needs can_revoke_grant on this ${kind} — grant-manager only, deliberately stricter than granting so a delegate cannot strip the owner who delegated to them.`,
				};
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
						options={GRANTABLE.map((r) => ({ value: r, label: RUNG_LABEL[r] ?? r }))}
					/>
					<!-- #143: a refused action stays VISIBLE and says why, rather than vanishing.
					     `disabled` is deliberately conditional on the verdict. GatedAction does not use the
					     native attribute — that would kill its tooltip and drop the control from the tab
					     order — so a natively-disabled child defeats the whole mechanism. When refused we
					     therefore leave the button enabled and let GatedAction's capture-phase guard block
					     the click; form-validity disabling still applies in the ALLOWED case, where it is
					     the only thing standing between the user and a pointless request. Same shape as
					     ProjectGallery's `New project`, the ruling's reference call site. -->
					<GatedAction allowed={mayGrant} action={`Grant ${mgRelation || 'a rung'}`} reason={grantReason}>
						<button
							class="btn"
							disabled={mayGrant && (mgBusy || !mgUser.trim() || !mgRelation)}
							onclick={() => runManage(true)}
						>
							{mgBusy ? '…' : 'Grant'}
						</button>
					</GatedAction>
					<GatedAction allowed={mayRevoke} action="Revoke" reason={revokeReason}>
						<button
							class="btn ghost"
							disabled={mayRevoke && (mgBusy || !mgUser.trim() || !mgRelation)}
							onclick={() => runManage(false)}
						>
							Revoke
						</button>
					</GatedAction>
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
