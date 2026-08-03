<!-- The app build (and svelte-check) compiles this WITHOUT customElement: true — correct, the app
     never mounts the wrapper; only vite.elements.config.ts compiles it as an element. -->
<!-- svelte-ignore options_missing_custom_element -->
<svelte:options customElement={{ shadow: 'none' }} />

<script lang="ts">
	/**
	 * `<rask-lakehouse-access>` — a READ-ONLY summary of the FGA explorer at
	 * /lakehouse/governance/access: the authorization MODEL (its id, its types, its conditions) and the
	 * LIVE tuple store the canvas rests on, with the page's own encodings intact — the facet rail's
	 * type/relation counts, the canvas legend's edge-kind colours (grant green, role/team amber, parent
	 * muted) and its `⏳ dashed = time-boxed` marker. The utility class strings ARE the page's, compiled
	 * into this bundle by elements.css since the host page's Tailwind build cannot generate them. The
	 * mount stamp + poll counter stay as the no-remount witness; tuple rows dispatch the rask:select
	 * contract event instead of seeding the canvas (a panel has no URL to write the query into).
	 *
	 * SCOPE, stated rather than implied. The page's transport converged on remote functions
	 * (`admin/remote/access.remote.ts`), which an element may not import — so this reads the same
	 * catalog through the zone's own root-absolute GET proxy (`/lakehouse/capi/**` →
	 * `makeCatalogProxy`, session bearer riding), which answers from ANY zone's page. That proxy is
	 * GET-only by design (the confused-deputy stance), and every interrogation surface the page
	 * offers — check, list-objects, list-users, expand, simulate — is a POST. So this panel carries the
	 * two GET reads (`/v1/access/model`, `/v1/access/tuples`) and NOT the query bar's verdicts: a
	 * "recent checks" list is impossible here twice over, since the catalog also exposes no endpoint
	 * that RECALLS past checks — they are evaluated, never recorded. The governance audit trail is the
	 * surface that does record, and it is already its own panel (`rask-lakehouse-audit`).
	 */
	import { Badge } from '@rask/ui/badge';
	import { Card } from '@rask/ui/card';
	import { ShieldAlert } from '@lucide/svelte';
	import { parse } from '@rask/api';
	import { RASK_SELECT, type SelectDetail } from '@rask/dockview/contract';
	import {
		AccessModelSchema,
		parseModelTypes,
		TuplesPageSchema,
		type AccessModel,
		type Tuple,
	} from '../admin/access';
	import { ElementPoll } from './poll.svelte';

	/** Page ceiling — the Explorer's `fetchStore` loop, smaller because this is a SUMMARY, not the
	 *  canvas: 8 × 100 = 800 tuples is already more rows than a panel can show, and a store past it is
	 *  reported as truncated exactly as the page does rather than silently clipped. */
	const MAX_PAGES = 8;

	type AccessSummary = {
		/** The catalog answered 401/403 — the store is estate-admin gated, which is a real answer and
		 *  not an outage. The page draws these as different states and so does this. */
		denied: boolean;
		model: AccessModel | null;
		tuples: Tuple[];
		truncated: boolean;
	};

	const DENIED: AccessSummary = { denied: true, model: null, tuples: [], truncated: false };

	let { pollms = 30000 }: { pollms?: number } = $props();
	const poll = new ElementPoll<AccessSummary>(async (f) => {
		const modelRes = await f('/lakehouse/capi/v1/access/model');
		if (modelRes.status === 401 || modelRes.status === 403) return DENIED;
		if (!modelRes.ok) throw new Error(`HTTP ${modelRes.status}`);
		const model = parse(AccessModelSchema, await modelRes.json());

		const tuples: Tuple[] = [];
		let continuation: string | null = null;
		let truncated = true;
		for (let page = 0; page < MAX_PAGES; page++) {
			const q = new URLSearchParams({ page_size: '100' });
			if (continuation) q.set('continuation', continuation);
			const res = await f(`/lakehouse/capi/v1/access/tuples?${q}`);
			if (res.status === 401 || res.status === 403) return DENIED;
			if (!res.ok) throw new Error(`HTTP ${res.status}`);
			const parsedPage = parse(TuplesPageSchema, await res.json());
			tuples.push(...parsedPage.tuples);
			continuation = parsedPage.continuation;
			if (!continuation) {
				truncated = false;
				break;
			}
		}
		return { denied: false, model, tuples, truncated };
	});
	$effect(() => poll.start(pollms));

	const denied = $derived(poll.data?.denied ?? false);
	const model = $derived(poll.data?.model ?? null);
	const tuples = $derived(poll.data?.tuples ?? []);
	const truncated = $derived(poll.data?.truncated ?? false);

	// ── Mirrored from lib/admin/access/graph.ts — the same tuple must be read the same way here, so
	// the panel's colours and counts cannot drift from the canvas they summarise. ──

	const idType = (id: string): string => id.split(':')[0] || 'unknown';

	/** A tuple subject may be a USERSET (`role:validators#assignee`); the canvas counts the ROLE's own
	 *  node, never one node per rung, so the rung is stripped before anything is counted. */
	const subjectNode = (id: string): string => {
		const hash = id.indexOf('#');
		return hash === -1 ? id : id.slice(0, hash);
	};

	type EdgeKind = 'grant' | 'membership' | 'structural';

	const edgeKind = (subject: string, label: string): EdgeKind => {
		if (label === 'parent') return 'structural';
		const type = idType(subject);
		if (subject.includes('#') || type === 'role' || type === 'team') return 'membership';
		return 'grant';
	};

	/** The canvas legend's colours, as text tones: grants green, role/team membership amber, `parent`
	 *  scaffolding muted. Colour is the code, and it says the same thing on both surfaces. */
	const KIND_TONE: Record<EdgeKind, string> = {
		grant: 'text-success',
		membership: 'text-warning',
		structural: 'text-muted-foreground',
	};

	/** Subjects (who) vs containers (what) — the model's own split. */
	const SUBJECT_TYPES = new Set(['user', 'team', 'role']);
	const isSubject = (id: string): boolean => SUBJECT_TYPES.has(idType(id));

	// ── the facet rail's two columns, computed exactly as buildWholeGraph does ──

	/** Every distinct NODE the store mentions — subjects (rung stripped) and objects alike. */
	const nodeIds = $derived.by(() => {
		const ids = new Set<string>();
		for (const t of tuples) {
			ids.add(subjectNode(t.user));
			ids.add(t.object);
		}
		return [...ids];
	});

	const subjectCount = $derived(nodeIds.filter(isSubject).length);
	const objectCount = $derived(nodeIds.length - subjectCount);

	/** Type counts over NODES — the facet rail's left column, alphabetical as it is there. */
	const typeCounts = $derived.by(() => {
		const counts = new Map<string, number>();
		for (const id of nodeIds) counts.set(idType(id), (counts.get(idType(id)) ?? 0) + 1);
		return [...counts.entries()].toSorted((a, b) => a[0].localeCompare(b[0]));
	});

	/** Relation counts over deduped EDGES — the rail's right column, and ranked by frequency for the
	 *  reason it gives there: `parent` dwarfs everything, and burying the rung an operator is looking
	 *  for under an alphabetical list is a worse default than density. */
	const relationCounts = $derived.by(() => {
		const counts = new Map<string, number>();
		const seen = new Set<string>();
		for (const t of tuples) {
			const id = `${subjectNode(t.user)}->${t.object}:${t.relation}`;
			if (seen.has(id)) continue;
			seen.add(id);
			counts.set(t.relation, (counts.get(t.relation) ?? 0) + 1);
		}
		return [...counts.entries()].toSorted((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]));
	});

	const modelTypes = $derived(model ? parseModelTypes(model.dsl) : []);
	const conditionNames = $derived(model ? Object.keys(model.conditions).toSorted() : []);
	/** Time-boxed grants, counted — the one fact the legend's `⏳` exists to make un-missable. */
	const conditionalCount = $derived(tuples.filter((t) => t.condition).length);

	function select(node: HTMLElement, t: Tuple) {
		node.dispatchEvent(
			new CustomEvent(RASK_SELECT, {
				bubbles: true,
				composed: true,
				detail: {
					source: 'rask-lakehouse-access',
					kind: 'fga-tuple',
					id: `${t.user}#${t.relation}@${t.object}`,
					label: `${t.user} ${t.relation} ${t.object}`,
				} satisfies SelectDetail,
			}),
		);
	}
</script>

<div class="bg-background block h-full overflow-auto p-3">
	<p class="text-muted-foreground mb-2 text-[11px]">mounted {poll.mountedAt} · poll #{poll.polls}</p>
	{#if poll.error !== null}
		<p class="text-destructive text-sm">Authorization store unavailable: {poll.error}</p>
	{:else if denied}
		<div class="flex items-center gap-2 rounded-md border border-border p-3 text-sm">
			<ShieldAlert size={16} /> The authorization store is estate-admin only.
		</div>
	{:else if poll.data === null}
		<p class="text-muted-foreground text-sm">Reading the authorization store…</p>
	{:else}
		<div class="flex flex-col gap-4 text-sm">
			<!-- Summary strip -->
			<section class="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
				<Card class="relative overflow-hidden p-4">
					<div class="absolute inset-x-0 top-0 h-0.5 bg-emerald-500"></div>
					<div class="text-muted-foreground text-[11px] font-medium tracking-wide uppercase">Tuples</div>
					<div class="mt-1 font-mono text-2xl tabular-nums">{tuples.length}</div>
					<div class="text-muted-foreground text-xs">
						{conditionalCount} time-boxed
					</div>
				</Card>

				<Card class="relative overflow-hidden p-4">
					<div class="absolute inset-x-0 top-0 h-0.5 bg-sky-500"></div>
					<div class="text-muted-foreground text-[11px] font-medium tracking-wide uppercase">
						Subjects
					</div>
					<div class="mt-1 font-mono text-2xl tabular-nums">{subjectCount}</div>
					<div class="text-muted-foreground text-xs">user · team · role</div>
				</Card>

				<Card class="relative overflow-hidden p-4">
					<div class="absolute inset-x-0 top-0 h-0.5 bg-violet-500"></div>
					<div class="text-muted-foreground text-[11px] font-medium tracking-wide uppercase">
						Objects
					</div>
					<div class="mt-1 font-mono text-2xl tabular-nums">{objectCount}</div>
					<div class="text-muted-foreground text-xs">everything granted on</div>
				</Card>

				<Card class="relative overflow-hidden p-4">
					<div class="absolute inset-x-0 top-0 h-0.5 bg-amber-500"></div>
					<div class="text-muted-foreground text-[11px] font-medium tracking-wide uppercase">
						Model types
					</div>
					<div class="mt-1 font-mono text-2xl tabular-nums">{modelTypes.length}</div>
					<div
						class="text-muted-foreground truncate text-xs"
						title={model?.authorization_model_id ?? ''}
					>
						{model?.authorization_model_id ?? '—'}
					</div>
				</Card>
			</section>

			{#if truncated}
				<p class="text-xs text-warning">
					The store is larger than this panel loads — the counts below are PARTIAL. Open the Access
					explorer to query for what you need.
				</p>
			{/if}

			<!-- The MODEL: what CAN grant what, ever — the page's "Model schema" half, minus the canvas. -->
			<Card class="overflow-hidden">
				<div
					class="text-muted-foreground border-b px-4 py-2 text-[11px] font-medium tracking-wide uppercase"
				>
					Model schema ({modelTypes.length})
				</div>
				<div class="flex flex-wrap items-center gap-1.5 p-3">
					{#each modelTypes as t (t)}
						<Badge variant="outline" class="text-muted-foreground px-1.5 py-px font-mono text-[10px]"
							>{t}</Badge
						>
					{:else}
						<span class="text-muted-foreground text-xs">The model declares no types.</span>
					{/each}
				</div>
				{#if conditionNames.length}
					<div class="border-t px-3 py-2">
						<span class="text-muted-foreground text-[11px]">conditions:</span>
						<span class="ml-1 inline-flex flex-wrap items-center gap-1">
							{#each conditionNames as c (c)}
								<Badge variant="secondary" class="px-1.5 py-px font-mono text-[10px]">{c}</Badge>
							{/each}
						</span>
					</div>
				{/if}
			</Card>

			<!-- The facet rail's two columns, read-only: what is on the canvas, by type and by relation. -->
			<section class="grid gap-3 sm:grid-cols-2">
				<Card class="overflow-hidden">
					<div
						class="text-muted-foreground border-b px-4 py-2 text-[11px] font-medium tracking-wide uppercase"
					>
						Type
					</div>
					<div class="flex flex-col gap-1 p-3">
						{#each typeCounts as [type, count] (type)}
							<div class="flex items-center gap-2 py-0.5 text-xs">
								<span class="flex-1 truncate font-mono">{type}</span>
								<span class="tabular-nums text-muted-foreground">{count}</span>
							</div>
						{:else}
							<p class="text-xs text-muted-foreground">Nothing in the store yet.</p>
						{/each}
					</div>
				</Card>

				<Card class="overflow-hidden">
					<div
						class="text-muted-foreground border-b px-4 py-2 text-[11px] font-medium tracking-wide uppercase"
					>
						Relation
					</div>
					<div class="flex flex-col gap-1 p-3">
						{#each relationCounts as [relation, count] (relation)}
							<div class="flex items-center gap-2 py-0.5 text-xs">
								<span class="flex-1 truncate font-mono">{relation}</span>
								<span class="tabular-nums text-muted-foreground">{count}</span>
							</div>
						{:else}
							<p class="text-xs text-muted-foreground">Nothing in the store yet.</p>
						{/each}
					</div>
				</Card>
			</section>

			<!-- The live store itself. Same three parts of a tuple, same encodings the canvas draws. -->
			<Card class="overflow-hidden">
				<div
					class="text-muted-foreground border-b px-4 py-2 text-[11px] font-medium tracking-wide uppercase"
				>
					Live tuples ({tuples.length})
				</div>
				<div class="max-h-full overflow-auto">
					<table class="w-full border-collapse text-xs">
						<thead class="bg-muted/50 sticky top-0 z-10 text-left">
							<tr class="border-b">
								<th class="text-muted-foreground px-3 py-2 font-medium">subject</th>
								<th class="text-muted-foreground px-3 py-2 font-medium">relation</th>
								<th class="text-muted-foreground px-3 py-2 font-medium">object</th>
							</tr>
						</thead>
						<tbody>
							{#each tuples as t (`${t.user}->${t.object}:${t.relation}`)}
								{@const kind = edgeKind(t.user, t.relation)}
								<tr
									class="hover:bg-muted/50 cursor-pointer border-b transition-colors"
									onclick={(e) => select(e.currentTarget, t)}
								>
									<td class="px-3 py-2 align-middle font-mono whitespace-nowrap">{t.user}</td>
									<td class="px-3 py-2 align-middle whitespace-nowrap">
										<span class="font-mono {KIND_TONE[kind]}">{t.relation}</span>
										{#if t.condition}
											<span
												class="text-muted-foreground"
												title={`time-boxed by ${t.condition.name}`}>⏳</span
											>
										{/if}
									</td>
									<td class="px-3 py-2 align-middle font-mono whitespace-nowrap">{t.object}</td>
								</tr>
							{:else}
								<tr>
									<td class="text-muted-foreground px-3 py-2" colspan="3">
										No tuples in the store — nothing is granted yet.
									</td>
								</tr>
							{/each}
						</tbody>
					</table>
				</div>
			</Card>

			<!-- The encodings, named — the canvas legend's own line, minus the canvas-only entries. -->
			<p class="text-muted-foreground text-[10px]">
				<span class="text-success">━</span> grant ·
				<span class="text-warning">━</span> role/team ·
				<span>─</span> parent ·
				<span>⏳</span> time-boxed
			</p>
		</div>
	{/if}
</div>
