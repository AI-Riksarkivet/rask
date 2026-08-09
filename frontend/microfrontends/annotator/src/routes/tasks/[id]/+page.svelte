<script lang="ts">
	// The project detail — A1's second half, hosting A2 (queue), A3 (review) and A4 (publish).
	// One load builds ONE snapshot (project + task details together), and every action refetches
	// it: the queue a reviewer is looking at is never computed from a different snapshot than the
	// publish precondition beside it.
	import { onMount } from 'svelte';
	import { base } from '$app/paths';
	import { page } from '$app/state';
	import { Badge } from '@rask/ui/badge';
	import { Button } from '@rask/ui/button';
	// NAMESPACE import, the estate's convention for a multi-part component (the same reason
	// QuickView documents for Sheet): the barrel also exports `Root as Tabs`, so `import { Tabs }`
	// compiles and then renders a bare Root where `Tabs.Content` was meant.
	import * as Tabs from '@rask/ui/tabs';
	import { ArrowLeft, Inbox, Pencil, RefreshCw } from '@lucide/svelte';

	import { fetchMeViaBff } from '$lib/http';
	import AdjudicationPanel from '$lib/projects/AdjudicationPanel.svelte';
	import AnnotatorMetrics from '$lib/projects/AnnotatorMetrics.svelte';
	import MembersPanel from '$lib/projects/MembersPanel.svelte';
	import PublishPanel from '$lib/projects/PublishPanel.svelte';
	import EditOntologyDialog from '$lib/projects/EditOntologyDialog.svelte';
	import SendItemsDialog from '$lib/projects/SendItemsDialog.svelte';
	import TaskQueue from '$lib/projects/TaskQueue.svelte';
	import { fetchProject, fireProjectEvent, listTasks } from '$lib/projects/remote/projects.remote';
	import { EVENT_LABELS, projectStateVariant } from '$lib/projects/presentation.js';
	import type { LegalEvent, ProjectDetail, TaskListing } from '$lib/projects/types.js';

	const projectId = $derived(page.params.id ?? '');

	let me = $state<string | null>(null);
	let detail = $state<ProjectDetail | null>(null);
	let listing = $state<TaskListing | null>(null);
	let status = $state<'loading' | 'ready' | 'forbidden' | 'missing' | 'offline'>('loading');
	let statusDetail = $state('');
	let notice = $state('');
	let sendOpen = $state(false);
	let ontologyOpen = $state(false);
	// ONE rule, named for what it means rather than for either of the two controls it gates: past
	// `frozen` a publish is being prepared against the answer set, so nothing may still change what
	// this project will publish. Editing the ontology and removing an item are both that.
	//
	// The server enforces each INDEPENDENTLY (`ONTOLOGY_EDITABLE_STATES`, `DROPPABLE_STATES`) — if
	// those two ever diverge, this splits with them. Hiding a control is presentation; the gate that
	// matters is the route's.
	const stillMutable = $derived(
		detail?.project.state === 'draft' || detail?.project.state === 'labeling',
	);
	let inflight = 0;

	async function load(): Promise<void> {
		const seq = ++inflight;
		// `refresh()`, not bare calls: a query instance is cached by its argument, so the publish
		// poll below — and every post-action refetch — would re-read the snapshot it already holds.
		const projectQuery = fetchProject({ projectId });
		const tasksQuery = listTasks({ projectId, details: true });
		try {
			await Promise.all([projectQuery.refresh(), tasksQuery.refresh()]);
		} catch (err) {
			// The ZONE SERVER is unreachable (the remote call itself failed, not the annotation service
			// behind it). Still the offline state — never a permanent "Loading labeling task…".
			if (seq !== inflight) return;
			detail = null;
			listing = null;
			status = 'offline';
			statusDetail = String(err);
			return;
		}
		if (seq !== inflight) return; // latest-wins
		const projectResult = projectQuery.current;
		const tasksResult = tasksQuery.current;
		if (projectResult === undefined) {
			detail = null;
			listing = null;
			status = 'offline';
			statusDetail = 'the labeling-task read returned nothing';
			return;
		}
		if (projectResult.ok) {
			detail = projectResult.data;
			listing = tasksResult?.ok ? tasksResult.data : null;
			status = 'ready';
		} else {
			detail = null;
			listing = null;
			status =
				projectResult.status === 403 || projectResult.status === 401
					? 'forbidden'
					: projectResult.status === 404
						? 'missing'
						: 'offline';
			statusDetail = projectResult.detail;
		}
	}

	// Identity once (it does not change with the route); the load re-runs per project param.
	onMount(() => {
		void (async () => {
			me = (await fetchMeViaBff())?.sub ?? null;
		})();
	});

	$effect(() => {
		void projectId; // the tracked dependency: re-load when the route param changes
		void load();
	});

	$effect(() => {
		if (detail?.project.state !== 'publishing') return;
		// POLL REASON: the publish saga runs server-side and narrates progress onto the project
		// document; there is no client event for "the saga advanced a step". 2 s tracks a run whose
		// steps take seconds, and the poll stops the moment the project leaves `publishing`.
		const timer = setInterval(() => void load(), 2000);
		return () => clearInterval(timer);
	});

	/** Header transitions: everything legal EXCEPT publish (the panel owns its confirm step) and
	 *  send (the dialog owns its form). */
	const headerEvents = $derived(
		(detail?.legal_events ?? []).filter(
			(e: LegalEvent) => e.event !== 'publish' && e.event !== 'send',
		),
	);
	const canSend = $derived(
		(detail?.legal_events ?? []).some((e: LegalEvent) => e.event === 'send'),
	);
	const canPublish = $derived(
		(detail?.legal_events ?? []).some((e: LegalEvent) => e.event === 'publish'),
	);

	let firing = $state(false);

	async function fireProject(event: string): Promise<void> {
		if (firing) return;
		firing = true;
		notice = '';
		const result = await fireProjectEvent({ projectId, event });
		firing = false;
		if (result.ok) {
			await load();
		} else {
			// 403 names the closed rung, 409 the illegal edge — the server's words, verbatim.
			notice = result.detail;
		}
	}
</script>

{#if status === 'loading'}
	<p class="text-muted-foreground p-6 text-sm">Loading labeling task…</p>
{:else if status === 'forbidden'}
	<div class="p-6 text-sm">
		<p class="font-medium">You can't view this labeling task.</p>
		<p class="text-muted-foreground mt-1">{statusDetail}</p>
	</div>
{:else if status === 'missing'}
	<div class="p-6 text-sm">
		<p class="font-medium">This labeling task does not exist.</p>
		<a class="text-muted-foreground mt-1 underline underline-offset-2" href="{base}/"
			>Back to labeling tasks</a
		>
	</div>
{:else if status === 'offline'}
	<div class="p-6 text-sm">
		<p class="font-medium">The annotation service is unreachable.</p>
		<p class="text-muted-foreground mt-1">{statusDetail}</p>
	</div>
{:else if detail}
	{@const project = detail.project}
	<!-- FULL WIDTH. This was `mx-auto max-w-6xl` — a 1152px cap with dead margin either side, and the
	     ONLY width-capped route in the estate (every other table page runs full-bleed). The queue is
	     a data table that has to carry the item's own columns; capping the page is what made it read
	     as squashed while the panels around it were fine. Prose blocks below keep their own
	     `max-w-3xl`, because a measure limit is right for TEXT and wrong for a table. -->
	<div class="flex w-full flex-col gap-4 p-6">
		<div class="flex flex-wrap items-center justify-between gap-3">
			<div class="flex items-center gap-3">
				<Button variant="ghost" size="sm" href="{base}/" aria-label="back to labeling tasks">
					<ArrowLeft class="size-4" />
				</Button>
				<div>
					<h1 class="flex items-center gap-2 text-lg font-semibold">
						{project.title || project.slug}
						<Badge variant={projectStateVariant(project.state)}>{project.state.replace('_', ' ')}</Badge>
					</h1>
					<p class="text-muted-foreground font-mono text-xs">{project.tenant} / {project.slug}</p>
				</div>
			</div>
			<div class="flex items-center gap-2">
				<Button variant="outline" size="sm" onclick={() => void load()}>
					<RefreshCw class="size-3.5" /> Refresh
				</Button>
				{#if canSend}
					<Button variant="outline" size="sm" onclick={() => (sendOpen = true)}>
						<Inbox class="size-3.5" />
						{EVENT_LABELS['send']}
					</Button>
				{/if}
				{#each headerEvents as legal (legal.event)}
					<Button
						variant="outline"
						size="sm"
						disabled={firing}
						onclick={() => void fireProject(legal.event)}
					>
						{EVENT_LABELS[legal.event] ?? legal.event}
					</Button>
				{/each}
			</div>
		</div>

		{#if project.description}
			<p class="text-muted-foreground max-w-3xl text-sm">{project.description}</p>
		{/if}
		{#if project.instructions}
			<!-- The annotator-facing HOW — rendered where the work is picked up, not buried in
			     settings. whitespace-pre-line keeps the author's line breaks without markdown. -->
			<div
				class="bg-muted/40 max-w-3xl rounded-md border px-3 py-2 text-sm"
				data-testid="instructions"
			>
				<span class="text-muted-foreground text-xs font-medium uppercase">Instructions</span>
				<p class="mt-1 whitespace-pre-line">{project.instructions}</p>
			</div>
		{/if}
		{#if notice}
			<p class="text-destructive text-sm">{notice}</p>
		{/if}

		<!-- THREE TABS, one per JOB: doing the labelling, configuring it, shipping it.
		     Reported: "why is this setting here? Access user:CiQwOGE4Njg0Yi1kYjg4… Should there not be
		     3 tabs.. labeling, task settings, publish". The page used to put the queue in one column and
		     stack Access + Publish + Adjudication in a 20rem sidebar beside it, so three unrelated
		     concerns shouted at once next to the work — and the taxonomy and its edit button sat in the
		     header above them, a fourth. None of those three is something you do WHILE labelling.
		     Tabbing them also retires the sidebar column that made the queue's table narrow: the queue
		     now owns the full width, which is the same defect the deleted comment below described
		     fixing once already by a different route (auto-placement putting the queue in a 320px
		     track). One surface per tab, and the surface you are on is the whole page. -->
		<Tabs.Root value="labeling">
			<Tabs.List>
				<Tabs.Trigger value="labeling" data-testid="tab-labeling">Labeling</Tabs.Trigger>
				<Tabs.Trigger value="settings" data-testid="tab-settings">Task settings</Tabs.Trigger>
				<Tabs.Trigger value="publish" data-testid="tab-publish">Publish</Tabs.Trigger>
			</Tabs.List>

			<!-- THE WORK. `min-w-0` so a wide table scrolls inside its own container rather than forcing
			     the panel wider than its parent (a flex/grid item's default `min-width:auto`). -->
			<Tabs.Content value="labeling" class="flex min-w-0 flex-col gap-4">
				<AnnotatorMetrics tasks={listing?.details ?? []} />
				<TaskQueue
					{projectId}
					droppable={stillMutable}
					tasks={listing?.details ?? []}
					{me}
					consensusN={project?.consensus_n ?? 1}
					adjudications={project?.adjudications ?? {}}
					onchanged={() => void load()}
				/>
			</Tabs.Content>

			<!-- WHAT the labelling job IS, and who may do it. The taxonomy moved here from the page
			     header: it is the task's DEFINITION, which is exactly what "task settings" names, and
			     leaving it up top meant the header carried settings while the sidebar carried more. -->
			<Tabs.Content value="settings" class="flex max-w-3xl flex-col gap-4">
				<!-- ONE block, because there is one document. This used to be two: a "Task type" chip fed
				     by `template` and a "Labeling" row fed by `label_schema`, which nothing cross-checked
				     — so the page could show a taxonomy the enforcement had never heard of. -->
				{#if project.ontology.kind}
					<div class="flex items-center gap-1.5 text-sm" data-testid="task-kind-chip">
						<span class="text-muted-foreground text-xs">Task type:</span>
						<Badge>{project.ontology.kind}</Badge>
					</div>
				{/if}
				{#if project.ontology.classes.length > 0}
					<div class="flex flex-wrap items-center gap-1.5 text-sm" data-testid="label-taxonomy">
						<span class="text-muted-foreground text-xs">Labeling:</span>
						{#each project.ontology.classes as labelClass (labelClass.name)}
							<Badge variant={labelClass.required ? 'default' : 'outline'}>
								{labelClass.name}
								{#if labelClass.tools.length > 0}
									<span class="text-muted-foreground">· {labelClass.tools.join('/')}</span>
								{/if}
								{#if labelClass.required}
									<!-- Per class, and only where it was actually declared. The create dialog used
									     to fill a project-level `required_labels` from EVERY class name, so adding a
									     third class silently made all three mandatory on every item. -->
									<span class="text-muted-foreground">· required</span>
								{/if}
							</Badge>
						{/each}
					</div>
				{/if}
				{#if stillMutable}
					<div>
						<Button
							variant="outline"
							size="sm"
							data-testid="edit-ontology-trigger"
							onclick={() => (ontologyOpen = true)}
						>
							<Pencil class="size-3.5" />
							{project.ontology.classes.length > 0
								? 'Edit the labeling task'
								: 'Define the labeling task'}
						</Button>
					</div>
				{/if}
				{#if project.ontology.relations.length > 0}
					<div class="flex flex-wrap items-center gap-1.5 text-sm" data-testid="relations">
						<span class="text-muted-foreground text-xs">Relations:</span>
						{#each project.ontology.relations as rel (rel.name)}
							<Badge variant="outline">{rel.name}</Badge>
						{/each}
					</div>
				{/if}
				<MembersPanel {projectId} />
			</Tabs.Content>

			<!-- SHIPPING it. Adjudication sits here rather than under Labeling because picking the
			     canonical replica is the gate publish waits on, not something an annotator does. -->
			<Tabs.Content value="publish" class="flex max-w-3xl flex-col gap-3">
				<PublishPanel {project} {listing} {canPublish} onchanged={() => void load()} />
				<AdjudicationPanel
					{projectId}
					tasks={listing?.details ?? []}
					adjudications={project?.adjudications ?? {}}
					onchanged={() => void load()}
				/>
				{#if (listing?.missing?.length ?? 0) > 0}
					<p class="text-warning text-xs">
						{listing?.missing?.length} item(s) are indexed but their actors hold no state — a re-send repairs
						them.
					</p>
				{/if}
			</Tabs.Content>
		</Tabs.Root>
	</div>

	<SendItemsDialog {projectId} bind:open={sendOpen} onsent={() => void load()} />
	{#if detail}
		<EditOntologyDialog
			{projectId}
			ontology={detail.project.ontology}
			bind:open={ontologyOpen}
			onsaved={() => void load()}
		/>
	{/if}
{/if}
