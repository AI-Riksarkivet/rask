<script lang="ts">
	// A1 · The projects landing — "your projects and their progress" (OPEN-WORK §Design, S9).
	// Replaces the DataSelection gallery as the zone's entry: items arrive by being SENT into a
	// project; the corpus browser stays reachable at /browse for picking what to send.
	import { onMount } from 'svelte';
	import { base } from '$app/paths';
	import { page } from '$app/state';
	import { Badge } from '@rask/ui/badge';
	import { Button } from '@rask/ui/button';
	import { Card } from '@rask/ui/card';
	import { Checkbox } from '@rask/ui/checkbox';
	import { Dialog } from '@rask/ui/dialog';
	import { Input } from '@rask/ui/input';
	import { Textarea } from '@rask/ui/textarea';
	import { Progress } from '@rask/ui/progress';
	import { Select } from '@rask/ui/select';
	import { projectFromHost } from '@rask/ui/shell';
	import { FolderPlus, RefreshCw } from '@lucide/svelte';

	import { fetchMeViaBff } from '$lib/http';
	import { createProject, listProjects } from './remote/projects.remote';
	import type { Project } from './types.js';
	import { projectStateVariant, taskProgress } from './presentation.js';
	import { PROJECT_TEMPLATES, templateById } from './templates.js';

	let tenant = $state('');
	let projects = $state<Project[]>([]);
	let status = $state<'loading' | 'ready' | 'forbidden' | 'offline'>('loading');
	let detail = $state('');

	let createOpen = $state(false);
	let creating = $state(false);
	let createError = $state('');
	let slug = $state('');
	let title = $state('');
	let description = $state('');
	let instructions = $state('');
	let reviewRequired = $state(true);
	// Consensus v1: how many INDEPENDENT annotators label each sent item. N>1 seeds N replica
	// items per item at send; one annotator may hold at most one replica of a group.
	let consensusN = $state(1);
	// The labeling TASK, specified at create — ONE document now (`LabelOntology`).
	//
	// This used to build TWO payloads from this same textarea: a `label_schema` of classes and a
	// `template` whose `required_labels` was *every* class name. Nothing cross-checked them, and the
	// second was a bug on its own — adding a third class silently made all three mandatory on every
	// item, because "the taxonomy" and "what is mandatory" were conflated. They are separate
	// properties in every product that has both, and they are separate here.
	let classesText = $state('');
	let shapeKind = $state('bbox');
	let classesRequired = $state(false);
	const SHAPE_KINDS = ['bbox', 'polygon', 'mask', 'segment', 'tag', 'text'];

	// PRESETS, aligned with Hugging Face pipeline ids by convention — `kind` is a free string
	// server-side, so this list is a starting vocabulary, not a closed enum. Picking one seeds the
	// tools each class gets; the enforceable contract is the classes themselves.
	let taskKind = $state('free');
	const TASK_PRESETS: Record<
		string,
		{ tools: string[]; attributes?: { name: string; type?: string; required?: boolean }[] }
	> = {
		'object-detection': { tools: ['bbox'] },
		'image-segmentation': { tools: ['polygon', 'mask'] },
		'image-classification': { tools: ['tag'] },
		'token-classification': { tools: ['text'] },
		'image-to-text': { tools: ['bbox', 'text'] },
		'document-question-answering': { tools: ['bbox', 'text'] },
		'reading-order': {
			tools: ['bbox'],
			attributes: [{ name: 'order', type: 'int', required: true }],
		},
	};

	const classNames = $derived(
		classesText
			.split(',')
			.map((c) => c.trim())
			.filter(Boolean),
	);

	// THE TEMPLATE GALLERY (finding 8). A template is a COMPLETE ontology — per-class tools,
	// typed attributes, relations — everything the free-form path below cannot author (it applies
	// one uniform tool list to every typed name). Picking one seeds the whole contract verbatim;
	// 'custom' keeps the free-form path exactly as it was.
	let templateId = $state('custom');
	const pickedTemplate = $derived(templateId === 'custom' ? undefined : templateById(templateId));

	// Derived, not a function: a pure projection of taskKind + classesText + the two toggles, which
	// is exactly what $derived is for. One payload — there is nothing left for a second to disagree
	// with.
	const ontologyPayload = $derived.by(() => {
		// A template wins whole: its per-class declarations are the point, and re-deriving them
		// from the comma list would collapse them back to uniform tools.
		if (pickedTemplate) return pickedTemplate.ontology;
		if (classNames.length === 0 && taskKind === 'free') return undefined;
		const preset = TASK_PRESETS[taskKind];
		// The preset's tools when one is picked, else the single shape kind. Per-class tools are
		// expressible in the model and NOT yet authorable here — a real per-class editor is its own
		// piece of work, so every class currently gets the same list.
		const tools = preset?.tools ?? [shapeKind];
		return {
			...(taskKind === 'free' ? {} : { kind: taskKind }),
			classes: classNames.map((name) => ({
				name,
				tools,
				attributes: preset?.attributes ?? [],
				required: classesRequired,
			})),
		};
	});

	async function resolveTenant(): Promise<string> {
		const me = await fetchMeViaBff();
		return me?.projects[0]?.project ?? projectFromHost(page.url.hostname) ?? 'default';
	}

	async function load(): Promise<void> {
		// `refresh()`, not a bare call: a query instance is cached by its argument, so re-awaiting it
		// for the Refresh button (or after a create) would hand back the list it already holds.
		const projectsQuery = listProjects({ tenant });
		try {
			await projectsQuery.refresh();
		} catch (err) {
			// The ZONE SERVER is unreachable (the remote call itself failed, not the annotation
			// service behind it). Still the offline card — never a permanent "Loading projects…".
			status = 'offline';
			detail = String(err);
			return;
		}
		const result = projectsQuery.current;
		if (result === undefined) {
			status = 'offline';
			detail = 'the projects read returned nothing';
			return;
		}
		if (result.ok) {
			projects = result.data.projects;
			status = 'ready';
		} else if (result.status === 403 || result.status === 401) {
			// Refused is REFUSED — rendering it as an empty list would read as "no projects".
			status = 'forbidden';
			detail = result.detail;
		} else {
			status = 'offline';
			detail = result.detail;
		}
	}

	// One-shot init (identity → tenant → list): onMount, not $effect — nothing here should re-run
	// on state changes, and the load-inside-effect shape reads as a reactivity smell it isn't.
	onMount(() => {
		void (async () => {
			tenant = await resolveTenant();
			await load();
		})();
	});

	async function create(): Promise<void> {
		if (!slug.trim() || creating) return;
		creating = true;
		createError = '';
		const result = await createProject({
			tenant,
			slug: slug.trim(),
			title: title.trim(),
			description: description.trim(),
			instructions: instructions.trim(),
			review_required: reviewRequired,
			consensus_n: Math.min(5, Math.max(1, Math.round(consensusN) || 1)),
			...(ontologyPayload ? { ontology: ontologyPayload } : {}),
		});
		creating = false;
		if (result.ok) {
			createOpen = false;
			slug = title = description = instructions = '';
			consensusN = 1;
			await load();
		} else {
			// 403 is a permission fact, not a form mistake — say which door closed.
			createError = result.detail;
		}
	}
</script>

<div class="mx-auto flex w-full max-w-5xl flex-col gap-4 p-6">
	<div class="flex items-center justify-between gap-3">
		<div>
			<h1 class="text-lg font-semibold">Labeling tasks</h1>
			<p class="text-muted-foreground text-sm">
				Tenant <span class="font-mono">{tenant || '…'}</span> — send items from
				<a class="underline underline-offset-2" href="{base}/browse">the corpus browser</a> or search, then
				claim, annotate, review and publish. (Projects are platform-level; labeling work lives here.)
			</p>
		</div>
		<div class="flex items-center gap-2">
			<Button variant="outline" size="sm" onclick={() => void load()}>
				<RefreshCw class="size-3.5" /> Refresh
			</Button>
			<Button size="sm" onclick={() => (createOpen = true)}>
				<FolderPlus class="size-3.5" /> New labeling task
			</Button>
		</div>
	</div>

	{#if status === 'loading'}
		<p class="text-muted-foreground text-sm">Loading projects…</p>
	{:else if status === 'forbidden'}
		<Card class="px-4 py-6 text-sm">
			<p class="font-medium">You can't list this tenant's labeling tasks.</p>
			<p class="text-muted-foreground mt-1">{detail}</p>
		</Card>
	{:else if status === 'offline'}
		<Card class="px-4 py-6 text-sm">
			<p class="font-medium">The annotation service is unreachable.</p>
			<p class="text-muted-foreground mt-1">{detail}</p>
		</Card>
	{:else if projects.length === 0}
		<Card class="flex flex-col items-start gap-2 px-4 py-8 text-sm">
			<p class="font-medium">No labeling tasks yet.</p>
			<p class="text-muted-foreground">
				Create one, then send items into it from <a
					class="underline underline-offset-2"
					href="{base}/browse">the corpus browser</a
				>.
			</p>
			<Button size="sm" class="mt-2" onclick={() => (createOpen = true)}>
				<FolderPlus class="size-3.5" /> New labeling task
			</Button>
		</Card>
	{:else}
		<div class="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
			{#each projects as project (project.project_id)}
				{@const progress = taskProgress(project.counts)}
				<a href="{base}/projects/{project.project_id}" class="group">
					<Card class="group-hover:border-ring flex h-full flex-col gap-2 p-4 transition-colors">
						<div class="flex items-start justify-between gap-2">
							<span class="text-sm font-semibold">{project.title || project.slug}</span>
							<Badge variant={projectStateVariant(project.state)}>{project.state.replace('_', ' ')}</Badge>
						</div>
						<span class="text-muted-foreground font-mono text-xs">{project.slug}</span>
						{#if project.description}
							<p class="text-muted-foreground line-clamp-2 text-xs">{project.description}</p>
						{/if}
						<Progress value={progress.done} max={Math.max(progress.total, 1)} />
						<p class="text-muted-foreground text-xs">
							{progress.done}/{progress.total} items terminal
							{#if project.publish_error}
								· <span class="text-destructive">publish failed</span>
							{:else if project.state === 'publishing'}
								· {project.publish_progress ?? 'publishing…'}
							{/if}
						</p>
					</Card>
				</a>
			{/each}
		</div>
	{/if}
</div>

<Dialog.Root bind:open={createOpen}>
	<!-- Scrollable: the create form grew past a laptop viewport (slug → title → description →
	     instructions → classes → shape kind → task type → consensus → review), which pushed the
	     submit button off-screen. A dialog taller than the window is broken for a user and for a
	     driver alike — the button is reachable only by a scroll that races every click. -->
	<Dialog.Content class="max-h-[85vh] overflow-y-auto sm:max-w-md">
		<Dialog.Title>New labeling task</Dialog.Title>
		<Dialog.Description>
			Born in <span class="font-mono">draft</span> — open it for labeling once items are sent.
		</Dialog.Description>
		<form
			class="flex flex-col gap-3"
			onsubmit={(e) => {
	e.preventDefault();
	void create();
}}
		>
			<label class="flex flex-col gap-1 text-sm">
				<span>Slug</span>
				<Input bind:value={slug} placeholder="vasa-portraits" required pattern="[a-z0-9-]+" />
			</label>
			<label class="flex flex-col gap-1 text-sm">
				<span>Title</span>
				<Input bind:value={title} placeholder="Vasa portraits" />
			</label>
			<label class="flex flex-col gap-1 text-sm">
				<span>Description</span>
				<Textarea bind:value={description} rows={3} placeholder="What is being labelled, and why" />
			</label>
			<label class="flex flex-col gap-1 text-sm">
				<span
					>Instructions <span class="text-muted-foreground">(shown to annotators — how to label)</span
					></span
				>
				<Textarea
					bind:value={instructions}
					rows={3}
					placeholder="Label every visible portrait; skip seals and marginalia."
				/>
			</label>
			<label class="flex flex-col gap-1 text-sm">
				<span
					>Template <span class="text-muted-foreground"
						>(a complete task — per-class tools, attributes, relations)</span
					></span
				>
				<Select
					bind:value={templateId}
					ariaLabel="Task template"
					options={[
	{ value: 'custom', label: 'custom (type your own classes)' },
	...PROJECT_TEMPLATES.map((t) => ({ value: t.id, label: t.name })),
]}
				/>
			</label>
			{#if pickedTemplate}
				<div
					class="border-border bg-muted/40 flex flex-col gap-1 rounded-md border p-2 text-xs"
					data-testid="template-summary"
				>
					<p class="text-muted-foreground">{pickedTemplate.description}</p>
					<p>
						{#each pickedTemplate.ontology.classes as c (c.name)}
							<span class="mr-1 inline-flex items-center gap-0.5">
								<Badge variant="outline">{c.name}</Badge>
								<span class="text-muted-foreground">[{c.tools.join(', ')}]</span>
							</span>
						{/each}
					</p>
					{#if pickedTemplate.ontology.relations?.length}
						<p class="text-muted-foreground">
							relations: {pickedTemplate.ontology.relations.map((r) => r.name).join(', ')}
						</p>
					{/if}
				</div>
			{:else}
				<label class="flex flex-col gap-1 text-sm">
					<span
						>Label classes <span class="text-muted-foreground">(comma-separated — the labeling task)</span
						></span
					>
					<Input bind:value={classesText} placeholder="person, ship, signature" />
				</label>
			{/if}
			{#if !pickedTemplate}
				<!-- The free-form knobs are the CUSTOM path's; a template already answered all three,
				     and dead controls under a picked template would advertise overrides that do not
				     apply. -->
				<label class="flex flex-col gap-1 text-sm">
					<span>Shape kind</span>
					<Select
						bind:value={shapeKind}
						ariaLabel="Shape kind"
						options={SHAPE_KINDS.map((kind) => ({ value: kind, label: kind }))}
					/>
				</label>
				<label class="flex flex-col gap-1 text-sm">
					<span
						>Task type <span class="text-muted-foreground"
							>(seeds each class's tools — enforced at submit)</span
						></span
					>
					<Select
						bind:value={taskKind}
						ariaLabel="Task type"
						options={[
	{ value: 'free', label: 'free (no task type)' },
	...Object.keys(TASK_PRESETS).map((kind) => ({ value: kind, label: kind })),
]}
					/>
				</label>
				<label class="flex items-center gap-2 text-sm">
					<Checkbox bind:checked={classesRequired} aria-label="Every class is required" />
					<span
						>Every class is required <span class="text-muted-foreground"
							>(each must appear on every completed item)</span
						></span
					>
				</label>
			{/if}
			<label class="flex flex-col gap-1 text-sm">
				<span
					>Consensus <span class="text-muted-foreground"
						>(annotators per item — 1 is ordinary, up to 5)</span
					></span
				>
				<Input type="number" bind:value={consensusN} min={1} max={5} step={1} />
			</label>
			<label class="flex items-center gap-2 text-sm">
				<Checkbox bind:checked={reviewRequired} />
				Review required before a task is accepted
			</label>
			{#if createError}
				<p class="text-destructive text-sm">{createError}</p>
			{/if}
			<div class="flex justify-end gap-2">
				<Button type="button" variant="outline" onclick={() => (createOpen = false)}>Cancel</Button>
				<Button type="submit" disabled={creating || !slug.trim()}>
					{creating ? 'Creating…' : 'Create labeling task'}
				</Button>
			</div>
		</form>
	</Dialog.Content>
</Dialog.Root>
