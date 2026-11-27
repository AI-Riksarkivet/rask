<script lang="ts">
	// `/namespaces/<id>` — the namespace detail page (sweep group 3): the catalog's namespace-scoped
	// governance surfaces (access list/check/grant/revoke + maintenance policy set/describe/delete).
	// Header (id + table count from the registry the /namespaces page already derives from), the
	// kind-generalized GrantsPanel, and the maintenance-policy section (`NamespacePolicy`). Same
	// stack-mode states as the registry — governed without a session ⇒ sign-in, unreachable ⇒ retrying.
	//
	// Every read is held through the read layer, one instance per namespace, so a navigation reads the
	// new namespace's instances with no latest-wins guard to hand-write; what the reader opened or typed
	// (the tab, the graph card, the policy editor) is keyed by the namespace, so none of it survives
	// into the next one.
	import { GrantsPanel, type GrantsClient } from '#lib/access/grants-panel/index.ts';
	import { Subject } from '#lib/access/identity/index.ts';
	import { Boxes, RefreshCw, ShieldAlert } from '@lucide/svelte';
	import { Button } from '@riksarkivet/ui/button';
	import { resolve } from '$app/paths';
	import { page } from '$app/state';
	import { untrack } from 'svelte';
	import { fetchTables } from '#lib/data/remote/catalog.api.ts';
	import DetailTabs from '#lib/data/DetailTabs.svelte';
	import NamespaceGraphCard from '#lib/data/NamespaceGraphCard.svelte';
	import NamespacePolicy from '#lib/data/NamespacePolicy.svelte';
	import ObjectLabels from '#lib/data/ObjectLabels.svelte';
	import { leafSegment, parentNamespace } from '#lib/api/identifiers.ts';
	import {
		checkAccess,
		fetchAccess,
		fetchAccessGraph,
		fetchManagedAccess,
		fetchMyPermissions,
		grantAccess,
		revokeAccess,
	} from '#lib/data/remote/access-objects.api.ts';
	import {
		describeNamespace,
		fetchNamespacePolicy,
		fetchNamespaceTasks,
	} from '#lib/data/remote/namespace.api.ts';
	import type { TrashEntry } from '#lib/data/remote/catalog.api.ts';
	import { formatZoned } from '@riksarkivet/ui/utils';
	import CatalogName from '#lib/data/CatalogName.svelte';
	import { fetchWarehouseNamespaces } from '#lib/data/remote/warehouses.api.ts';
	import ConnectSnippets from '#lib/data/ConnectSnippets.svelte';
	import { catalogPath } from '#lib/catalog-trail.ts';

	const ns = $derived(page.params.id ?? '');

	// The FGA view lives on an Access TAB (overview stays the default), and the overview carries the
	// namespace's labels. The choice holds for the namespace it was made on.
	let tabChoice = $state<{ for: string; value: string } | null>(null);
	const tab = $derived(tabChoice?.for === ns ? tabChoice.value : 'overview');

	// The catalog calls the grants panel makes, bound to this page's reads and writes: the panel's
	// positional signature onto the data functions' single argument.
	const grantsClient: GrantsClient = {
		fetchAccess: (kind, id) => fetchAccess({ kind, id }),
		checkAccess: (kind, id, user, relation) => checkAccess({ kind, id, user, relation }),
		grantAccess: (kind, id, user, relation) => grantAccess({ kind, id, user, relation }),
		revokeAccess: (kind, id, user, relation) => revokeAccess({ kind, id, user, relation }),
		// #143: the panel renders a refused Grant/Revoke DISABLED WITH ITS REASON rather than letting
		// the user discover the denial from a 403. It needs the caller's own verdicts to do that, and
		// the console owns the transport — so the seam gains a fifth member like the four above.
		fetchMyPermissions: (kind, id) => fetchMyPermissions({ kind, id }),
	};

	// The registry, the describe and the child listing go out together on each namespace the page shows.
	// A read held in a derivation goes out when the markup first reads it, and each of these is read only
	// in a branch another's answer decides, so held apart they would go out one after another. The child
	// listing is skipped while the describe's kept answer already says this caller cannot see the
	// namespace (its listing would be refused), and asked for once a fresh describe says it can. The
	// registry is one shared instance, so the call re-reads it unless a read is out or just settled.
	const reads = $derived.by(() => {
		const describe = describeNamespace(ns);
		const kept = untrack(() => describe.current);
		return {
			tables: fetchTables(),
			describe,
			children: kept === undefined || kept.ok ? fetchWarehouseNamespaces(ns) : null,
		};
	});
	const tablesRead = $derived(reads.tables);
	const tablesAnswer = $derived(tablesRead.current);
	const tables = $derived(tablesAnswer?.ok ? tablesAnswer.data.tables : null);
	const lastStatus = $derived(
		tablesAnswer === undefined ? 0 : tablesAnswer.ok ? 200 : tablesAnswer.status,
	);
	const unauthorized = $derived(tables === null && lastStatus === 401);
	const offline = $derived(tables === null && tablesAnswer !== undefined && lastStatus !== 401);

	// The namespace's member tables, grouped the way the registry groups them — through the SAME helper
	// (`parentNamespace`, every segment but the last), not a local re-implementation of it: a copy that
	// split on the FIRST delimiter would compare a nested namespace's top-level ancestor against `ns`.
	const members = $derived((tables ?? []).filter((t) => parentNamespace(t) === ns).sort());

	/** When a trash record's drop was made; a record that keeps no drop time sorts before every other. */
	const droppedMs = (entry: TrashEntry): number =>
		entry.dropped_at == null ? -Infinity : Date.parse(entry.dropped_at);

	/** The newest of a name's trash records: a name dropped twice keeps a record per drop. */
	const newestDrop = (entries: readonly TrashEntry[]): TrashEntry | undefined =>
		entries.reduce<TrashEntry | undefined>(
			(newest, entry) =>
				newest === undefined || droppedMs(entry) > droppedMs(newest) ? entry : newest,
			undefined,
		);

	// Whether the namespace is there before its governance is read. A describe the catalog refuses (403)
	// or does not find (404) is a namespace this caller cannot see: dropped, never there, or theirs to see
	// no more. Its trash record, when the caller may read one, says it was dropped; without one the page
	// says it cannot show the namespace, and asks for none of its governance, every read of which would
	// be refused. A describe that failed any other way (a 5xx, an unreachable catalog) claims nothing.
	// The read is held in its own derivation and read in another: a read created inside the derivation
	// that reads it would not be tracked by it.
	const describeRead = $derived(reads.describe);
	const described = $derived(describeRead.current);
	const refused = $derived(
		described !== undefined && !described.ok && [403, 404].includes(described.status),
	);
	const tasksRead = $derived(refused ? fetchNamespaceTasks(ns) : null);
	type Presence =
		| { kind: 'checking' }
		| { kind: 'live' }
		| { kind: 'dropped'; entry: TrashEntry }
		| { kind: 'not-visible' }
		| { kind: 'unavailable'; status: number; detail: string };
	const presence = $derived.by((): Presence => {
		if (described === undefined) return { kind: 'checking' };
		if (described.ok) return { kind: 'live' };
		if (!refused)
			return { kind: 'unavailable', status: described.status, detail: described.detail };
		const tasks = tasksRead?.current;
		if (tasks === undefined) return { kind: 'checking' };
		const entry = tasks.ok ? newestDrop(tasks.data) : undefined;
		return entry ? { kind: 'dropped', entry } : { kind: 'not-visible' };
	});
	const live = $derived(presence.kind === 'live');

	/** The namespaces directly under this one (the spec's ListNamespaces on its id), so a table in a nested
	 *  namespace is reached from its warehouse through every namespace above it. `null`: the listing failed,
	 *  said so on the page, never read as "none". */
	const childrenRead = $derived(reads.children ?? (live ? fetchWarehouseNamespaces(ns) : null));
	const children = $derived.by((): string[] | null => {
		const res = childrenRead?.current;
		if (res === undefined) return [];
		return res.ok ? [...res.data.namespaces].sort() : null;
	});

	// The governance reads, issued together, in this order, and only for a namespace the caller can
	// see: the policy; what the SIGNED-IN caller may do here (the catalog's self-view: a single-table
	// grantee reaches this page through upward visibility, so an ungated `Set policy` would be a button
	// that exists solely to fail); and whether granting here is CENTRALIZED (`perms` says what the caller
	// may do, this says WHY, since an owner whose grant controls vanished is otherwise looking at a bug).
	const governance = $derived(
		live
			? {
					policy: fetchNamespacePolicy({ namespace: ns }),
					perms: fetchMyPermissions({ kind: 'namespace', id: ns }),
					managed: fetchManagedAccess({ kind: 'namespace', id: ns }),
				}
			: null,
	);
	// `null` means "not answered yet", which is NOT "denied": treating it as denied would hide an
	// owner's own controls on every first paint. A failed read stays unanswered.
	const perms = $derived.by(() => {
		const res = governance?.perms.current;
		return res?.ok ? res.data.permissions : null;
	});
	const managed = $derived.by(() => {
		const res = governance?.managed.current;
		return res?.ok ? res.data.managed_access : null;
	});
	// The one case worth naming separately: the caller owns this namespace, and granting is still
	// denied. That is managed access doing its job.
	const grantsCentralized = $derived(managed === true && perms?.can_delete === true);

	// #81 the graph card, open only for the namespace it was opened on; each opening reads the graph.
	let graphFor = $state<string | null>(null);
	const showGraph = $derived(graphFor === ns);
	const graphRead = $derived(showGraph ? fetchAccessGraph({ kind: 'namespace', id: ns }) : null);
</script>

<svelte:head><title>{catalogPath(ns)} · namespaces · rask</title></svelte:head>

<div class="page">
	<header>
		<Boxes size={15} />
		<CatalogName id={ns} kind="namespace" />
		<!-- Counted only for a namespace the caller can see: a count beside "no namespace you can see"
		     would describe one. -->
		{#if tables !== null && presence.kind === 'live'}
			<span class="count">{members.length} table{members.length === 1 ? '' : 's'}</span>
		{/if}
	</header>

	{#if unauthorized}
		<div class="empty">
			<ShieldAlert size={16} />
			<p>
				This stack is governed — <a
					href={resolve(`auth/login?redirect=${encodeURIComponent(page.url.pathname)}`)}>sign in</a
				> to view this namespace.
			</p>
		</div>
	{:else if offline}
		<div class="empty">
			<RefreshCw size={16} />
			<p>Catalog unreachable (HTTP {lastStatus}).</p>
		</div>
	{:else if presence.kind === 'dropped'}
		<div class="dropped" data-testid="namespace-dropped">
			<h2>This namespace was dropped</h2>
			<p>
				Dropped by <Subject value={presence.entry.dropped_by} /> on
				{formatZoned(presence.entry.dropped_at)}. It stays recoverable until
				<strong>{formatZoned(presence.entry.expires_at)}</strong>; its tables went with it.
			</p>
		</div>
	{:else if presence.kind === 'not-visible'}
		<div class="empty" data-testid="namespace-not-visible">
			<p>
				No namespace {ns} that you can see: it does not exist, or you hold no grant on it.
			</p>
		</div>
	{:else if presence.kind === 'unavailable'}
		<div class="empty">
			<RefreshCw size={16} />
			<p>Could not read namespace {ns} (HTTP {presence.status}): {presence.detail}</p>
		</div>
	{:else if tables === null || presence.kind === 'checking'}
		<div class="empty"><p>Loading…</p></div>
	{:else}
		<!-- Goal cond 3: overview (default) | connect | access. -->
		<DetailTabs
			tabs={['overview', 'connect', 'access']}
			active={tab}
			onselect={(next) => (tabChoice = { for: ns, value: next })}
		/>
		<!-- The overview is HIDDEN on the other tabs, not unmounted, so a policy draft being typed
		     survives a trip to another tab; `{#key ns}` still resets it on a namespace navigation. -->
		<div hidden={tab !== 'overview'}>
			{#key ns}
				<ObjectLabels kind="namespace" id={ns} />
			{/key}
			{#if children === null}
				<section>
					<h2>Namespaces</h2>
					<p class="mut">The namespaces under this one could not be listed.</p>
				</section>
			{:else if children.length > 0}
				<section>
					<h2>Namespaces</h2>
					<ul class="list">
						{#each children as child (child)}
							<li>
								<a
									class="row mono"
									href={resolve('/catalog/namespaces/[id]', { id: encodeURIComponent(child) })}
									>{leafSegment(child)}</a
								>
							</li>
						{/each}
					</ul>
				</section>
			{/if}
			<section>
				<h2>Tables</h2>
				{#if members.length === 0}
					<p class="mut">No tables in this namespace that are visible to you.</p>
				{:else}
					<ul class="list">
						{#each members as t (t)}
							<li>
								<a
									class="row mono"
									href={resolve('/catalog/tables/[table]', { table: encodeURIComponent(t) })}
									>{leafSegment(t)}</a
								>
							</li>
						{/each}
					</ul>
				{/if}
			</section>

			{#if governance}
				{#key ns}
					<NamespacePolicy {ns} read={governance.policy} {perms} />
				{/key}
			{/if}
		</div>
		{#if tab === 'connect'}
			<ConnectSnippets id={ns} kind="namespace" />
		{:else if tab === 'access'}
			<section>
				<h2>Access</h2>
				<!-- Managed access is a POLICY, and an unexplained absence is indistinguishable from a
				     bug. Shown only once the read has landed and only when it actually explains
				     something the caller can see: they own this namespace and granting is still denied.
				     Anyone without the owner rung already gets the rung message and does not need a
				     second reason. -->
				{#if grantsCentralized}
					<p class="mut managed">
						Granting here is <strong>centralized</strong>. Owners of this namespace keep every other
						power and cannot hand out access; a grant-manager on the warehouse above does that.
						Clearing it is theirs too — a policy you can switch off from inside is not a policy.
					</p>
				{/if}
				<GrantsPanel dataset={ns} kind="namespace" client={grantsClient} />
				<!-- #81 one hop of the authorization graph, read each time it is opened. -->
				<Button
					variant="ghost"
					size="sm"
					class="mt-2.5 mb-2"
					onclick={() => (graphFor = showGraph ? null : ns)}
				>
					{showGraph ? 'Hide' : 'Show'} authorization graph
				</Button>
				{#if graphRead}<NamespaceGraphCard {ns} read={graphRead} />{/if}
			</section>
		{/if}
	{/if}
</div>

<style>
	.page {
		max-width: 860px;
		margin: 0 auto;
		padding: 56px 20px 40px;
	}
	header {
		display: flex;
		align-items: center;
		gap: 8px;
		margin-bottom: 18px;
		color: var(--muted-foreground);
	}
	.count {
		font-size: 12px;
		color: var(--muted-foreground);
	}
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

	.managed {
		border-left: 2px solid var(--border);
		padding-left: 8px;
		color: var(--muted-foreground);
	}
	.list {
		list-style: none;
		margin: 0;
		padding: 0;
	}
	.row {
		display: block;
		padding: 6px 10px;
		border-bottom: 1px solid color-mix(in srgb, var(--border) 45%, transparent);
		color: var(--foreground);
		text-decoration: none;
		font-size: 13px;
	}
	.row:hover {
		background: var(--muted);
	}
	.dropped {
		border: 1px solid color-mix(in srgb, var(--warning) 45%, var(--border));
		border-radius: var(--radius-sm);
		padding: 16px 18px;
		margin: 24px 0;
		font-size: 13px;
	}
	.dropped h2 {
		margin: 0 0 6px;
	}
	.empty {
		display: flex;
		align-items: center;
		gap: 8px;
		color: var(--muted-foreground);
		padding: 32px 0;
	}
</style>
