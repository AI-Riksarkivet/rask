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
	import { ArrowLeft, Inbox, RefreshCw } from '@lucide/svelte';

	import { fetchMeViaBff } from '$lib/http';
	import AdjudicationPanel from '$lib/projects/AdjudicationPanel.svelte';
	import PublishPanel from '$lib/projects/PublishPanel.svelte';
	import SendItemsDialog from '$lib/projects/SendItemsDialog.svelte';
	import TaskQueue from '$lib/projects/TaskQueue.svelte';
	import { projectsApi } from '$lib/projects/client.js';
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
	let inflight = 0;

	async function load(): Promise<void> {
		const seq = ++inflight;
		const [projectResult, tasksResult] = await Promise.all([
			projectsApi.get(projectId),
			projectsApi.listTasks(projectId, { details: true }),
		]);
		if (seq !== inflight) return; // latest-wins
		if (projectResult.ok) {
			detail = projectResult.data;
			listing = tasksResult.ok ? tasksResult.data : null;
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
		const result = await projectsApi.fireEvent(projectId, event);
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
	<p class="text-muted-foreground p-6 text-sm">Loading project…</p>
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
	<div class="mx-auto flex w-full max-w-6xl flex-col gap-4 p-6">
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
		{#if project.template?.enforce}
			<div class="flex items-center gap-1.5 text-sm" data-testid="template-chip">
				<span class="text-muted-foreground text-xs">Task type:</span>
				<Badge>{project.template.kind}</Badge>
				<span class="text-muted-foreground text-xs"
					>tools: {project.template.tools.join(', ')} — enforced at submit</span
				>
			</div>
		{/if}
		{#if project.label_schema.classes.length > 0}
			<!-- The labeling TASK, visible where the work happens: what is being labelled, with what
			     geometry. Publish stamps the same list into table properties + the lineage facet. -->
			<div class="flex flex-wrap items-center gap-1.5 text-sm" data-testid="label-taxonomy">
				<span class="text-muted-foreground text-xs">Labeling:</span>
				{#each project.label_schema.classes as labelClass (labelClass.name)}
					<Badge variant="outline">
						{labelClass.name}
						{#if labelClass.shape_types.length > 0}
							<span class="text-muted-foreground">· {labelClass.shape_types.join('/')}</span>
						{/if}
					</Badge>
				{/each}
			</div>
		{/if}
		{#if notice}
			<p class="text-destructive text-sm">{notice}</p>
		{/if}

		<div class="grid grid-cols-1 gap-4 lg:grid-cols-[minmax(0,1fr)_20rem]">
			<TaskQueue
				tasks={listing?.details ?? []}
				{me}
				consensusN={project?.consensus_n ?? 1}
				adjudications={project?.adjudications ?? {}}
				onchanged={() => void load()}
			/>
			<div class="flex flex-col gap-3">
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
			</div>
		</div>
	</div>

	<SendItemsDialog {projectId} bind:open={sendOpen} onsent={() => void load()} />
{/if}
