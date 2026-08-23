<script lang="ts">
	// The send half of the annotation funnel: a selection made HERE (search hits, an atlas lasso)
	// becomes tasks in an annotation project — appended to a project that is still taking items
	// (draft/labeling), or a new project created around the selection. The annotator zone's landing
	// is the other end; nothing is annotated in media.
	import { page } from '$app/state';
	import { Badge } from '@rask/ui/badge';
	import { Button } from '@rask/ui/button';
	import { Dialog } from '@rask/ui/dialog';
	import { Input } from '@rask/ui/input';
	import { Select } from '@rask/ui/select';
	import { predictionFor } from '$lib/atlas/atlas-send';
	import { projectFromHost } from '@rask/ui/shell';
	import type { ProjectRow, SendItem } from '$lib/projects/projects';
	import {
		createProject,
		listProjects,
		sendProjectItems,
	} from '$lib/projects/remote/projects.remote';

	let {
		open = $bindable(false),
		keys,
		dataset,
		origin = 'search',
		datasetVersion = null,
		labelChoices = [],
	}: {
		open?: boolean;
		/** Descriptor key-paths (`doc/speech/chunk`) — what the selection IS. */
		keys: string[];
		/** The dataset the keys came from — informational provenance on each task. */
		dataset: string | null;
		/** The dataset's version AT SEND TIME (§4.5's reproducibility capture) — when every item of a
		 *  publish shares one dataset at one version, the publish pins it and lineage gains its READ
		 *  edge. Null = uncaptured (honest: no pin is fabricated). */
		datasetVersion?: number | null;
		/** Which surface sent — search results or an atlas selection (provenance only). */
		origin?: 'search' | 'atlas';
		/** Class names this selection may be labelled with — the chosen project's taxonomy. Empty
		 *  means "no taxonomy to offer", and the picker is then absent rather than empty. */
		labelChoices?: string[];
	} = $props();

	// EMPTY STRING is "no label", because `Select` declares `value = $bindable('')` — a bindable with
	// a fallback cannot be bound to `undefined`, and doing so throws `props_invalid_value` at RENDER.
	let label = $state('');

	let projects = $state<ProjectRow[]>([]);
	let loadState = $state<'loading' | 'ready' | 'error'>('loading');
	let loadDetail = $state('');
	let sending = $state(false);
	let error = $state('');
	let done = $state<{ projectId: string; created: number; sent: number } | null>(null);

	let newSlug = $state('');
	let newTitle = $state('');

	const tenant = $derived(projectFromHost(page.url.hostname) ?? 'default');
	/** Only projects still TAKING items — `send` is legal in draft/labeling alone (§5.1). */
	const openProjects = $derived(
		projects.filter((p) => p.state === 'draft' || p.state === 'labeling'),
	);

	async function loadProjects(): Promise<void> {
		loadState = 'loading';
		try {
			// A query is CACHED per argument, and opening the dialog has always meant "read the list
			// now" — a project created or published since the last open must not still be in the
			// picker. `refresh()` is what makes the re-open a re-read rather than a replay.
			const list = listProjects({ tenant });
			await list.refresh();
			const res = await list;
			if (!res.ok) {
				loadState = 'error';
				loadDetail = res.detail;
				return;
			}
			projects = res.data.projects;
			loadState = 'ready';
		} catch (err) {
			// The zone server itself is unreachable (an offline tab) — the upstream's own refusals come
			// back as `ok: false` above and never land here.
			loadState = 'error';
			loadDetail = String(err);
		}
	}

	// Opening the dialog IS the load trigger — an async fetch has no $derived form; the effect
	// tracks `open` alone and resets the per-open outcome state before fetching.
	$effect(() => {
		if (!open) return;
		done = null;
		error = '';
		void loadProjects();
	});

	function items(): SendItem[] {
		// The bulk LABEL, as one whole-item tag. Absent when nothing was picked — that is the ordinary
		// send ("queue these for someone to draw on") and must stay byte-identical.
		const prediction = predictionFor(label);
		return keys.map((key) => ({
			source: {
				kind: 'chunks' as const,
				keys: [key],
				where: dataset,
				dataset_version: datasetVersion,
			},
			media: { kind: 'image' as const },
			...(prediction ? { prediction } : {}),
		}));
	}

	async function appendTo(projectId: string): Promise<void> {
		if (sending) return;
		sending = true;
		error = '';
		try {
			const res = await sendProjectItems({ projectId, items: items() });
			// The server's refusal verbatim: 403 names the missing rung, 409 names a closed project.
			if (!res.ok) {
				error = res.detail;
				return;
			}
			done = { projectId, created: res.data.created ?? 0, sent: res.data.sent ?? keys.length };
		} catch (err) {
			error = String(err instanceof Error ? err.message : err);
		} finally {
			sending = false;
		}
	}

	async function createAndSend(): Promise<void> {
		if (sending || !newSlug.trim()) return;
		sending = true;
		error = '';
		try {
			const created = await createProject({
				tenant,
				slug: newSlug.trim(),
				title: newTitle.trim(),
			});
			if (!created.ok || !created.data.project_id) {
				error = created.ok ? 'the annotator created no project' : created.detail;
				return;
			}
			sending = false;
			await appendTo(created.data.project_id);
		} catch (err) {
			error = String(err instanceof Error ? err.message : err);
		} finally {
			sending = false;
		}
	}
</script>

<Dialog.Root bind:open>
	<Dialog.Content class="sm:max-w-md">
		<Dialog.Title
			>{label
				? `Label ${keys.length} item${keys.length === 1 ? '' : 's'} as ${label}`
				: `Send ${keys.length} item${keys.length === 1 ? '' : 's'} to a labeling task`}</Dialog.Title
		>
		<Dialog.Description>
			From {origin === 'atlas' ? 'the atlas selection' : 'the search results'}
			{#if dataset}&nbsp;on <span class="font-mono">{dataset}</span>{/if} — each item becomes a claimable
			task; provenance (what was sent, from where, by whom) travels with it.
		</Dialog.Description>

		{#if done}
			<div class="flex flex-col gap-2 text-sm" data-testid="send-done">
				<p>
					Sent <span class="font-medium">{done.sent}</span> item{done.sent === 1 ? '' : 's'}
					({done.created} new item{done.created === 1 ? '' : 's'} — re-sent items are not duplicated).
				</p>
				<a
					class="underline underline-offset-2"
					href="/annotator/projects/{done.projectId}"
					data-sveltekit-reload>Open the labeling task in the annotator →</a
				>
			</div>
		{:else}
			<div class="flex flex-col gap-3 text-sm">
				<!-- LABEL the selection, optionally. A region of an embedding projection is usually a
				     semantic cluster, so one lasso is very often one class — but sending unlabelled
				     ("queue these for someone to draw on") is the ordinary case and stays the default.
				     Rendered only when a taxonomy was supplied: an empty picker reads as "this project
				     has no classes" rather than "nothing to choose from". -->
				{#if labelChoices.length > 0}
					<label class="flex flex-col gap-1">
						<span class="text-muted-foreground text-xs">Label every item as…</span>
						<Select
							bind:value={label}
							ariaLabel="Label to apply"
							placeholder="Send unlabelled"
							options={labelChoices.map((name) => ({ value: name, label: name }))}
						/>
					</label>
				{/if}

				{#if loadState === 'loading'}
					<p class="text-muted-foreground">Loading projects…</p>
				{:else if loadState === 'error'}
					<p class="text-destructive">Could not list projects: {loadDetail}</p>
				{:else if openProjects.length === 0}
					<p class="text-muted-foreground">
						No labeling task is taking items right now — create one below.
					</p>
				{:else}
					<ul class="flex max-h-56 flex-col gap-1 overflow-y-auto" data-testid="send-project-list">
						{#each openProjects as project (project.project_id)}
							<li>
								<button
									type="button"
									class="hover:bg-muted flex w-full items-center justify-between gap-2 rounded-md border px-3 py-2 text-left disabled:opacity-50"
									disabled={sending}
									onclick={() => void appendTo(project.project_id)}
								>
									<span class="flex min-w-0 flex-col">
										<span class="truncate font-medium">{project.title || project.slug}</span>
										<span class="text-muted-foreground truncate font-mono text-xs"
											>{project.slug}</span
										>
									</span>
									<Badge variant={project.state === 'labeling' ? 'default' : 'secondary'}>
										{project.state}
									</Badge>
								</button>
							</li>
						{/each}
					</ul>
				{/if}

				<div class="border-border flex flex-col gap-2 border-t pt-3">
					<p class="text-muted-foreground text-xs">
						…or create a new labeling task around this selection:
					</p>
					<div class="flex gap-2">
						<Input bind:value={newSlug} placeholder="slug (vasa-portraits)" pattern="[a-z0-9-]+" />
						<Input bind:value={newTitle} placeholder="Title" />
					</div>
					<Button
						size="sm"
						class="w-fit"
						disabled={sending || !newSlug.trim()}
						onclick={() => void createAndSend()}
					>
						{sending ? 'Sending…' : `Create & send ${keys.length}`}
					</Button>
				</div>

				{#if error}
					<p class="text-destructive" data-testid="send-error">{error}</p>
				{/if}
			</div>
		{/if}
	</Dialog.Content>
</Dialog.Root>
