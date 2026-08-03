<script lang="ts">
	// The send half of the annotation funnel: a selection made HERE (search hits, an atlas lasso)
	// becomes tasks in an annotation project — appended to a project that is still taking items
	// (draft/labeling), or a new project created around the selection. The annotator zone's landing
	// is the other end; nothing is annotated in media.
	import { base } from '$app/paths';
	import { page } from '$app/state';
	import { Badge } from '@rask/ui/badge';
	import { Button } from '@rask/ui/button';
	import { Dialog } from '@rask/ui/dialog';
	import { Input } from '@rask/ui/input';
	import { projectFromHost } from '@rask/ui/shell';

	let {
		open = $bindable(false),
		keys,
		dataset,
		origin = 'search',
		datasetVersion = null,
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
	} = $props();

	type ProjectRow = {
		project_id: string;
		slug: string;
		title: string;
		state: string;
		counts: Record<string, number>;
	};

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
			const res = await fetch(`${base}/api/projects?tenant=${encodeURIComponent(tenant)}`);
			if (!res.ok) throw new Error(`HTTP ${res.status}`);
			projects = ((await res.json()) as { projects: ProjectRow[] }).projects;
			loadState = 'ready';
		} catch (err) {
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

	function items(): unknown[] {
		return keys.map((key) => ({
			source: { kind: 'chunks', keys: [key], where: dataset, dataset_version: datasetVersion },
			media: { kind: 'image' },
		}));
	}

	async function appendTo(projectId: string): Promise<void> {
		if (sending) return;
		sending = true;
		error = '';
		try {
			const res = await fetch(`${base}/api/projects/${projectId}/items`, {
				method: 'POST',
				headers: { 'content-type': 'application/json' },
				body: JSON.stringify({ items: items() }),
			});
			const body = (await res.json()) as { detail?: string; sent?: number; created?: number };
			if (!res.ok) throw new Error(body.detail ?? `HTTP ${res.status}`);
			done = { projectId, created: body.created ?? 0, sent: body.sent ?? keys.length };
		} catch (err) {
			// The server's refusal verbatim: 403 names the missing rung, 409 names a closed project.
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
			const created = await fetch(`${base}/api/projects`, {
				method: 'POST',
				headers: { 'content-type': 'application/json' },
				body: JSON.stringify({ tenant, slug: newSlug.trim(), title: newTitle.trim() }),
			});
			const project = (await created.json()) as { detail?: string; project_id?: string };
			if (!created.ok || !project.project_id)
				throw new Error(project.detail ?? `HTTP ${created.status}`);
			sending = false;
			await appendTo(project.project_id);
		} catch (err) {
			error = String(err instanceof Error ? err.message : err);
			sending = false;
		}
	}
</script>

<Dialog.Root bind:open>
	<Dialog.Content class="sm:max-w-md">
		<Dialog.Title
			>Send {keys.length} item{keys.length === 1 ? '' : 's'} to a labeling task</Dialog.Title
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
										<span class="text-muted-foreground truncate font-mono text-xs">{project.slug}</span>
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
