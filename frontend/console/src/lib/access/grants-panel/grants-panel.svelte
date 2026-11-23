<script lang="ts">
	// The access-review panel: who holds which can_* action on a catalog object, expanded through the
	// FGA model (roles, nested roles, the parent cascade). `kind` picks the object: a table (the
	// default), a namespace, a warehouse or a project. The catalog gates the enumeration, so a caller
	// who may not review sees the denial, never the ACL.
	// The page hands in its `client`, the catalog calls bound to its own reads and writes, so the panel
	// owns the flow and the page the transport.
	// Collapsed by default: one review round-trip per object, with definitive outcomes (the ACL,
	// 401/403/406) cached and transient failures (offline, 5xx) retried on the next open.
	import { ChevronRight, ShieldCheck } from '@lucide/svelte';
	import { buttonVariants } from '@riksarkivet/ui/button';
	import * as Collapsible from '@riksarkivet/ui/collapsible';
	import { cn } from '@riksarkivet/ui/utils';
	import type { ApiResult } from '#lib/api/client.ts';
	import type {
		AccessCheck,
		AccessGrant,
		AccessKind,
		AccessList,
		GrantKind,
		MyPermissions,
	} from '#lib/data/namespace.ts';
	import { hasAccessCheck } from '#lib/data/namespace.ts';
	import { GrantForm } from '../grant-form/index.ts';
	import Subject from '../identity/subject.svelte';
	import AccessCheckForm from './access-check.svelte';

	/** The catalog calls the panel makes: the review, a check, and grant/revoke, each gated by the
	 *  catalog. */
	export type GrantsClient = {
		fetchAccess: (kind: GrantKind, id: string) => Promise<ApiResult<AccessList>>;
		/** Only a table and a namespace have the check door, so the simulator renders only for them. */
		checkAccess?: (
			kind: AccessKind,
			id: string,
			user: string,
			relation: string,
		) => Promise<ApiResult<AccessCheck>>;
		grantAccess: (
			kind: GrantKind,
			id: string,
			user: string,
			relation: string,
		) => Promise<ApiResult<AccessGrant>>;
		revokeAccess: (
			kind: GrantKind,
			id: string,
			user: string,
			relation: string,
		) => Promise<ApiResult<AccessGrant>>;
		/** The caller's own verdicts on this object (`{ can_grant_reader: true, … }`), so a refused
		 *  Grant or Revoke renders disabled with its reason. Optional: absent means unknown, which
		 *  renders the controls live, so a verdict is never invented from a read that was not made. */
		fetchMyPermissions?: (kind: GrantKind, id: string) => Promise<ApiResult<MyPermissions>>;
	};

	let {
		dataset,
		kind = 'table',
		client,
	}: { dataset: string; kind?: GrantKind; client: GrantsClient } = $props();

	// The catalog's review gate per kind: `can_review_access` on a table or a namespace,
	// `can_read_assignments` on a warehouse (its grant managers, owners included unless access is
	// centrally managed) and on a project (its security admins, every admin among them).
	const REVIEW_DENIED: Record<GrantKind, string> = {
		table: 'Owner access required to review who can reach this table.',
		namespace: 'Owner access required to review who can reach this namespace.',
		warehouse:
			'Only those who manage this warehouse’s grants, and its project’s admins, may see who has access here.',
		project:
			'Only this project’s admins and security admins may see or change who has access here.',
	};

	const checkClient = $derived(hasAccessCheck(kind) ? (client.checkAccess ?? null) : null);

	// #143: the caller's own verdicts, KEYED BY DATASET like `review.for` below.
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
	let failedDetail = $state('');

	// #72 manage-access form — grant/revoke a base rung to a subject.
	let mgUser = $state('');

	// The access check's draft subject and relation. Its form is unmounted with the collapsible, so the
	// draft lives here, keyed by the object: closing and reopening the review keeps it, and another
	// object starts blank.
	let checkDraft = $state<{ for: string; user: string; relation: string } | null>(null);
	const checkUser = $derived(checkDraft?.for === dataset ? checkDraft.user : '');
	const checkRelation = $derived(checkDraft?.for === dataset ? checkDraft.relation : '');

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
			} else if (res.status === 401 || res.status === 403 || res.status === 406) {
				const denied =
					res.status === 401
						? 'Sign in to review access.'
						: res.status === 403
							? REVIEW_DENIED[kind]
							: 'This stack runs auth-off — there are no grants to review.';
				review = { for: current, access: null, denied };
			} else {
				failedFor = current; // offline / 5xx: shown but not cached, so the next open retries
				failedDetail = res.detail;
			}
		} finally {
			if (loadingFor === current) loadingFor = null;
		}
	}

	// Hide the empty rows: a relation nobody holds is noise in a review of who has access.
	const held = $derived(shown?.access ? shown.access.grants.filter((g) => g.users.length > 0) : []);
	// Every can_* action the model defines on this type (incl. ones nobody holds) — the options you can
	// simulate, taken from the unfiltered grants the review already fetched.
	const relations = $derived(shown?.access ? shown.access.grants.map((g) => g.relation) : []);

	// #72 grant or revoke a base rung, then re-fetch the review so the change is visible immediately.
	// Named rather than inlined into `onmutated=` deliberately: this comment described a handler that
	// had been moved into the markup, so it documented nothing, and `rsvelte-fmt` reformats a
	// multi-statement arrow inside a markup attribute to column 0 — which is what made this file the
	// estate's only `fmt:check` failure.
	async function refreshAfterMutation() {
		// The object the write was on: the page may show another by the time the read is answered.
		const written = dataset;
		const refreshed = await client.fetchAccess(kind, written);
		if (refreshed.ok && dataset === written) {
			review = { for: written, access: refreshed.data, denied: null };
		}
	}
</script>

<!-- A disclosure: opening it is what starts the review, so `onOpenChange` runs `toggle`, and `open`
     stays the one keyed by the dataset. -->
<Collapsible.Root
	{open}
	onOpenChange={() => void toggle()}
	class="mt-0.5 mb-2.5 flex flex-col gap-1.5"
>
	<Collapsible.Trigger
		class={cn(
			buttonVariants({ variant: 'ghost', size: 'sm' }),
			'group/review text-muted-foreground self-start',
		)}
	>
		<ChevronRight
			size={12}
			class="transition-transform duration-(--duration-fast) group-data-[state=open]/review:rotate-90"
		/>
		<ShieldCheck size={12} />
		<span>Access review</span>
	</Collapsible.Trigger>
	<Collapsible.Content class="flex flex-col gap-1.5">
		{#if loading}
			<p class="mut">Reviewing access…</p>
		{:else if failed}
			<p class="mut">
				Access review unavailable right now{failedDetail ? `: ${failedDetail}` : ''} — close and reopen
				to retry.
			</p>
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

			{#if checkClient && hasAccessCheck(kind)}
				<AccessCheckForm
					{kind}
					{dataset}
					{relations}
					check={checkClient}
					bind:user={
						() => checkUser,
						(user) => (checkDraft = { for: dataset, user, relation: checkRelation })
					}
					bind:relation={
						() => checkRelation,
						(relation) => (checkDraft = { for: dataset, user: checkUser, relation })
					}
				/>
			{/if}

			<div class="border-border/55 mt-2.5 border-t pt-2">
				<div class="text-muted-foreground mb-1.5 text-[11px] tracking-wider uppercase">
					Manage access (grant / revoke)
				</div>
				<!-- The grant form the access graph renders too. `knownSubjects` comes from the review this
				     panel already renders, the only subject directory there is. -->
				<!-- Keyed by the object: a rung picked for one is no draft for the next. -->
				{#key dataset}
					<GrantForm
						{kind}
						bind:subject={mgUser}
						permissions={permMap}
						knownSubjects={(shown?.access?.grants ?? []).flatMap((g) => g.users)}
						grant={(user, relation) => client.grantAccess(kind, dataset, user, relation)}
						revoke={(user, relation) => client.revokeAccess(kind, dataset, user, relation)}
						onmutated={refreshAfterMutation}
					/>
				{/key}
			</div>
		{/if}
	</Collapsible.Content>
</Collapsible.Root>

<style>
	.mut {
		color: var(--muted-foreground);
		font-size: 12px;
		margin: 0;
	}
	.acl {
		border-collapse: collapse;
		font-size: 12px;
	}
	.acl th {
		text-align: left;
		color: var(--muted-foreground);
		font-weight: 500;
		padding: 2px 14px 2px 0;
	}
	.acl td {
		padding: 2px 14px 2px 0;
		vertical-align: top;
	}
	.rel {
		color: var(--muted-foreground);
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
		background: var(--muted);
		border: 1px solid var(--border);
		border-radius: var(--radius-sm);
		padding: 0 6px;
		margin: 0 4px 3px 0;
	}
	.who.wild {
		border-color: color-mix(in srgb, var(--warning) 55%, var(--border));
	}
</style>
