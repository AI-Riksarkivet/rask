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
	// The labeling TASK, specified at create: what is being labelled (class names) and with what
	// geometry. This is `LabelSchema` (§4.1) — the publish stamps it into the table properties and
	// the lineage facet.
	//
	// The claim that "the canvas constrains its tools to it" used to sit here and was FALSE: the
	// rail showed all ten tools regardless, so a violation was discovered at submit, after the work.
	// The canvas now honours the TEMPLATE's tools (AnnotatorToolbar's `visible`, proven by
	// viewer/allowed-tools.test.ts) — the template, not this taxonomy, because the template is what
	// the server enforces. That split is itself the defect LabelOntology exists to close.
	let classesText = $state('');
	let shapeKind = $state('bbox');
	const SHAPE_KINDS = ['bbox', 'polygon', 'mask', 'segment', 'tag', 'text'];
	// Task templates v1 — the LS-config idea as PRESETS: pick a task type and the template's
	// tools/attributes come with it, ENFORCED server-side at submit. 'free' keeps today's
	// unconstrained behavior (enforce=false), so nothing existing changes.
	let templateKind = $state('free');
	const TEMPLATE_PRESETS: Record<
		string,
		{
			tools: string[];
			attributes?: { name: string; type?: string; choices?: string[]; required?: boolean }[];
		}
	> = {
		'bbox-detection': { tools: ['bbox'] },
		segmentation: { tools: ['polygon', 'mask'] },
		classification: { tools: ['tag'] },
		'text-span': { tools: ['text'] },
		transcription: { tools: ['bbox', 'text'] },
		'doc-qa': { tools: ['bbox', 'text'] },
		'reading-order': {
			tools: ['bbox'],
			attributes: [{ name: 'order', type: 'int', required: true }],
		},
	};

	// Derived, not a function: the payload is a pure projection of templateKind + classesText,
	// which is exactly what $derived is for (svelte-runes: computed → $derived, const = read-only).
	const templatePayload = $derived.by(() => {
		const preset = TEMPLATE_PRESETS[templateKind];
		if (!preset) return undefined; // 'free' — the unconstrained default
		return {
			kind: templateKind,
			tools: preset.tools,
			// The taxonomy doubles as the output contract: every listed class must appear.
			required_labels: classesText
				.split(',')
				.map((c) => c.trim())
				.filter(Boolean),
			attributes: preset.attributes ?? [],
			enforce: true,
		};
	});

	function labelSchema(): { classes: { name: string; shape_types: string[] }[] } | undefined {
		const names = classesText
			.split(',')
			.map((c) => c.trim())
			.filter(Boolean);
		if (names.length === 0) return undefined;
		return { classes: names.map((name) => ({ name, shape_types: [shapeKind] })) };
	}

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
		const schema = labelSchema();
		const result = await createProject({
			tenant,
			slug: slug.trim(),
			title: title.trim(),
			description: description.trim(),
			instructions: instructions.trim(),
			review_required: reviewRequired,
			consensus_n: Math.min(5, Math.max(1, Math.round(consensusN) || 1)),
			...(schema ? { label_schema: schema } : {}),
			...(templatePayload ? { template: templatePayload } : {}),
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
					>Label classes <span class="text-muted-foreground">(comma-separated — the labeling task)</span
					></span
				>
				<Input bind:value={classesText} placeholder="person, ship, signature" />
			</label>
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
						>(a template — its tools and outputs are enforced at submit)</span
					></span
				>
				<Select
					bind:value={templateKind}
					ariaLabel="Task type"
					options={[
	{ value: 'free', label: 'free (no template)' },
	...Object.keys(TEMPLATE_PRESETS).map((kind) => ({ value: kind, label: kind })),
]}
				/>
			</label>
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
