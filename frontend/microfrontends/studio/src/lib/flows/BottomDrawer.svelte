<script lang="ts">
	/** The bottom drawer — the run LOG, the saved-flow library and the API snippet, in an
	 *  editor-style bar that collapses to a single row.
	 *
	 *  All three lived in the floating top-left toolbar first, which grew to ~550 px tall with the
	 *  snippet open and covered the canvas it was floating over. A drawer is the right shape: it is
	 *  out of the way when closed, it spans the width so a log line or a curl command has room to be
	 *  read, and it does not overlap the graph at all.
	 *
	 *  Resizing is hand-rolled rather than `@rask/ui`'s `ResizableSplit` (which the inspector pane
	 *  uses, and which does support `orientation="vertical"`) for ONE reason: that component's second
	 *  pane has a hard `minRight`, so it cannot collapse to a bar — and switching between a split and
	 *  a non-split layout would remount the canvas and lose the viewport. Same interaction, same
	 *  persistence, one fewer remount. */
	import { onMount } from 'svelte';
	import { on } from 'svelte/events';
	import {
		ChevronDown,
		ChevronUp,
		Code2,
		Copy,
		FolderOpen,
		Save,
		ScrollText,
		Server,
		Trash2,
	} from '@lucide/svelte';
	import { Button } from '@rask/ui/button';
	import { graph } from '$lib/flows/graph.svelte';
	import { logs } from '$lib/flows/logs.svelte';
	import { library } from '$lib/flows/library.svelte';
	import { buildRunRequest, curlSnippet, pythonSnippet } from '$lib/flows/api-snippet';
	import { getFlowsEngine } from '$lib/flows/remote/engine.remote';

	const engineQuery = getFlowsEngine();
	const engine = $derived(engineQuery.current);

	type Tab = 'logs' | 'flows' | 'api';

	const HEIGHT_KEY = 'studio-flow-drawer-h';
	const MIN_H = 96;
	const MAX_H = 640;
	const DEFAULT_H = 220;

	let open = $state(false);
	let tab = $state<Tab>('logs');
	let height = $state(DEFAULT_H);

	// Hydrate the persisted height ONCE on the client. `onMount`, not `$effect`: this is a one-shot
	// read of non-reactive storage that seeds a value the user then drags, so an effect would only
	// imply a dependency that does not exist. (`ResizableSplit` uses an effect for the same job; the
	// intent reads better this way.)
	onMount(() => {
		try {
			const v = localStorage.getItem(HEIGHT_KEY);
			const n = v === null ? NaN : parseInt(v, 10);
			if (!Number.isNaN(n)) height = Math.min(MAX_H, Math.max(MIN_H, n));
		} catch {
			// storage disabled — the default height is fine
		}
	});

	// ── Drag the top edge ─────────────────────────────────────────────────────
	let dragging = $state(false);
	function startDrag(e: PointerEvent): void {
		dragging = true;
		const startY = e.clientY;
		const startH = height;
		const el = e.currentTarget as HTMLElement;
		// Pointer capture, so a fast drag that leaves the 4-px handle keeps resizing.
		el.setPointerCapture(e.pointerId);
		// `on` from svelte/events rather than addEventListener (oxlint's svelte plugin): each call
		// returns its own off function, so the two are released together on pointerup.
		const offMove = on(el, 'pointermove', (ev) => {
			// Dragging UP grows the drawer, so the delta is inverted.
			height = Math.min(MAX_H, Math.max(MIN_H, startH + (startY - ev.clientY)));
		});
		const offUp = on(el, 'pointerup', () => {
			dragging = false;
			offMove();
			offUp();
			try {
				localStorage.setItem(HEIGHT_KEY, String(height));
			} catch {
				// best-effort, exactly like the graph autosave
			}
		});
	}

	// ── Saved flows ───────────────────────────────────────────────────────────
	let flowName = $state('');
	let loadFailed = $state(false);
	function saveFlow(): void {
		if (library.save(flowName || library.current, graph.snapshot())) flowName = library.current;
	}
	function openFlow(name: string): void {
		const snap = library.load(name);
		loadFailed = !(snap && graph.loadSnapshot(snap));
		if (!loadFailed) flowName = name;
	}

	// ── API export ────────────────────────────────────────────────────────────
	let apiLang = $state<'curl' | 'python'>('curl');
	const exported = $derived(buildRunRequest(graph.nodes, graph.edges, graph.config));
	const snippet = $derived(
		apiLang === 'curl' ? curlSnippet(exported.body) : pythonSnippet(exported.body),
	);

	const LEVEL_CLASS = {
		info: 'text-muted-foreground',
		warn: 'text-amber-600 dark:text-amber-400',
		error: 'text-destructive',
	} as const;

	/** Open the drawer on `t`; clicking the ACTIVE tab of an open drawer closes it (one control for
	 *  both directions, the way an editor's panel tabs behave). */
	function pick(t: Tab): void {
		if (open && tab === t) open = false;
		else {
			tab = t;
			open = true;
		}
	}
</script>

<section
	class="border-border bg-card flex shrink-0 flex-col border-t"
	style:height={open ? `${height}px` : 'auto'}
	aria-label="Flow output"
>
	{#if open}
		<!-- Resize handle. `h-1.5` with a wider hit area via padding would fight the drawer's own
		     layout, so the row itself is the target and the cursor advertises it. -->
		<!-- svelte-ignore a11y_no_static_element_interactions -->
		<div
			class="hover:bg-primary/40 h-1 shrink-0 cursor-row-resize {dragging ? 'bg-primary/60' : ''}"
			onpointerdown={startDrag}
		></div>
	{/if}

	<!-- The bar: always visible, so the drawer is discoverable when closed. -->
	<div class="flex shrink-0 items-center gap-1 px-2 py-1">
		{#each [{ id: 'logs', label: 'Logs', icon: ScrollText }, { id: 'flows', label: 'Flows', icon: FolderOpen }, { id: 'api', label: 'API', icon: Code2 }] as const as t (t.id)}
			<button
				class="flex items-center gap-1 rounded px-1.5 py-0.5 text-xs {open && tab === t.id
					? 'bg-muted text-foreground'
					: 'text-muted-foreground hover:text-foreground'}"
				onclick={() => pick(t.id)}
			>
				<t.icon class="size-3" />
				{t.label}
				{#if t.id === 'logs' && logs.problemCount > 0}
					<span
						class="bg-destructive/15 text-destructive rounded px-1 text-[0.6rem] font-medium"
						title="{logs.problemCount} warning(s)/error(s)">{logs.problemCount}</span
					>
				{/if}
			</button>
		{/each}
		<div class="flex-1"></div>
		{#if graph.lastError}
			<span class="text-destructive truncate text-[0.7rem]">{graph.lastError}</span>
		{/if}
		<!-- The flows service owns server-side (durable) runs; v0 executes in the browser either way,
		     so its absence is informational rather than an error state. -->
		<span
			class="text-muted-foreground/80 flex items-center gap-1 text-[0.7rem]"
			title="The flows service will own server-side (durable) runs on Ray; v0 executes in the browser either way"
		>
			<Server class="size-3" />
			{#if engine?.ok}
				engine online · {engine.kinds} kinds
			{:else}
				engine offline
			{/if}
		</span>
		<button
			class="text-muted-foreground hover:text-foreground rounded p-0.5"
			title={open ? 'Collapse the drawer' : 'Expand the drawer'}
			aria-label={open ? 'Collapse the drawer' : 'Expand the drawer'}
			onclick={() => (open = !open)}
		>
			{#if open}<ChevronDown class="size-3.5" />{:else}<ChevronUp class="size-3.5" />{/if}
		</button>
	</div>

	{#if open}
		<div class="min-h-0 flex-1 overflow-hidden">
			{#if tab === 'logs'}
				<div class="flex h-full min-h-0 flex-col">
					<div
						class="text-muted-foreground flex shrink-0 items-center gap-2 px-2 pb-1 text-[0.7rem]"
					>
						<label class="flex items-center gap-1">
							<input type="checkbox" bind:checked={logs.problemsOnly} />
							problems only
						</label>
						<div class="flex-1"></div>
						<button
							class="hover:text-foreground rounded p-0.5"
							title="Copy the whole log"
							aria-label="Copy log"
							onclick={() => void navigator.clipboard.writeText(logs.asText())}
						>
							<Copy class="size-3" />
						</button>
						<button
							class="hover:text-foreground rounded p-0.5"
							title="Clear the log"
							aria-label="Clear log"
							onclick={() => logs.clear()}
						>
							<Trash2 class="size-3" />
						</button>
					</div>
					<div
						class="min-h-0 flex-1 overflow-auto px-2 pb-2 font-mono text-[0.7rem] leading-relaxed"
					>
						{#each logs.visible as e (e.seq)}
							<div class="flex gap-2">
								<span class="text-muted-foreground/60 shrink-0">{e.at.toTimeString().slice(0, 8)}</span>
								{#if e.nodeId}
									<span class="text-primary/80 shrink-0">{e.nodeId}</span>
								{/if}
								<span class="{LEVEL_CLASS[e.level]} break-words">{e.message}</span>
							</div>
						{/each}
						{#if logs.visible.length === 0}
							<p class="text-muted-foreground/70">
								{logs.entries.length ? 'No problems logged.' : 'Run the flow — every call lands here.'}
							</p>
						{/if}
					</div>
				</div>
			{:else if tab === 'flows'}
				<div class="flex h-full min-h-0 flex-col gap-2 px-2 pb-2">
					<div class="flex items-center gap-1.5">
						<input
							class="border-border bg-background text-foreground focus:border-primary w-40 rounded border px-1.5 py-1 text-xs outline-none"
							placeholder="flow name"
							aria-label="Flow name"
							bind:value={flowName}
						/>
						<Button size="sm" variant="outline" onclick={saveFlow}>
							<Save class="size-3.5" />
							Save
						</Button>
						{#if library.error}
							<span class="text-destructive text-[0.7rem]">{library.error}</span>
						{/if}
						{#if loadFailed}
							<span class="text-destructive text-[0.7rem]">
								That flow could not be read — it may predate a schema change.
							</span>
						{/if}
					</div>
					<div class="min-h-0 flex-1 overflow-auto">
						{#each library.flows as f (f.name)}
							<div
								class="hover:bg-muted/60 flex items-center gap-2 rounded px-1 py-0.5 text-xs {library.current ===
								f.name
									? 'bg-muted'
									: ''}"
							>
								<button class="min-w-0 flex-1 truncate text-left" onclick={() => openFlow(f.name)}>
									{f.name}
								</button>
								<span class="text-muted-foreground/70 shrink-0 text-[0.65rem]">
									{f.savedAt ? f.savedAt.slice(0, 16).replace('T', ' ') : ''}
								</span>
								<button
									class="text-muted-foreground/60 hover:text-destructive shrink-0 rounded p-0.5"
									title="Forget “{f.name}”"
									aria-label="Delete {f.name}"
									onclick={() => library.remove(f.name)}
								>
									<Trash2 class="size-3" />
								</button>
							</div>
						{/each}
						{#if library.flows.length === 0}
							<p class="text-muted-foreground/70 text-[0.7rem]">
								No saved flows yet — name this one and press Save.
							</p>
						{/if}
					</div>
				</div>
			{:else}
				<div class="flex h-full min-h-0 flex-col gap-1 px-2 pb-2">
					<div class="flex shrink-0 items-center gap-1">
						{#each ['curl', 'python'] as const as lang (lang)}
							<button
								class="rounded px-1.5 py-0.5 text-[0.7rem] {apiLang === lang
									? 'bg-primary text-primary-foreground'
									: 'text-muted-foreground hover:text-foreground'}"
								onclick={() => (apiLang = lang)}
							>
								{lang}
							</button>
						{/each}
						<div class="flex-1"></div>
						{#if exported.droppedImageNodes.length}
							<span class="text-[0.65rem] text-amber-600 dark:text-amber-400">
								image node(s) {exported.droppedImageNodes.join(', ')} omitted — an upload lives in the browser
							</span>
						{/if}
						<button
							class="text-muted-foreground/60 hover:text-foreground rounded p-0.5"
							title="Copy the snippet"
							aria-label="Copy snippet"
							onclick={() => void navigator.clipboard.writeText(snippet)}
						>
							<Copy class="size-3" />
						</button>
					</div>
					<pre
						class="border-border bg-background min-h-0 flex-1 overflow-auto rounded border p-1.5 font-mono text-[0.7rem] leading-snug whitespace-pre-wrap">{snippet}</pre>
				</div>
			{/if}
		</div>
	{/if}
</section>
