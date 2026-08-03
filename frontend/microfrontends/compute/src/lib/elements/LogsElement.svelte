<!-- The app build (and svelte-check) compiles this WITHOUT customElement: true — correct, the app
     never mounts the wrapper; only vite.elements.config.ts compiles it as an element. -->
<!-- svelte-ignore options_missing_custom_element -->
<svelte:options customElement={{ shadow: 'none' }} />

<script lang="ts">
	/**
	 * `<rask-compute-logs>` — the Ray log viewer, VISUALLY IDENTICAL to the zone's /compute/logviewer
	 * page: the same toolbar (node + line-count selects, follow, refresh, in-log search with
	 * match-only, wrap / line numbers / copy / download), the same Card-bounded file sidebar with its
	 * category headers, and the same monospace line list with the page's level colouring and <mark>
	 * highlighting — the same utility classes, compiled into this bundle by elements.css since the
	 * host page's Tailwind cannot generate them. The mount stamp + poll counter stay as the no-remount
	 * witness; a file row dispatches the rask:select contract event as well as opening the file.
	 *
	 * The page reads through remote functions; an element cannot ($app/* and .remote are impossible
	 * cross-zone), so the same three reads go through @rask/api's root-absolute `/api/ray/logs`
	 * clients: the node list on the shared RayPoll clock (mirroring the page's 5 s cluster refresh),
	 * the file inventory and the tail re-read by $effect on node / filename / line-count change —
	 * which is exactly what the page's param queries do when they re-key.
	 *
	 * Deliberately NOT mirrored: the page's `?node`/`?file`/`?q` deep links (a panel has no URL).
	 */
	import { Card } from '@rask/ui/card';
	import {
		rayCluster,
		rayLogContent,
		rayLogFiles,
		type RayClusterPayload,
		type RayNode,
	} from '@rask/api';
	import { timeoutSignal } from '@rask/api/client';
	import { RASK_SELECT, type SelectDetail } from '@rask/dockview/contract';
	import { RayPoll } from './ray-poll.svelte';
	import {
		RefreshCw,
		FileText,
		Play,
		Pause,
		Search,
		Copy,
		Download,
		TextWrap,
		Hash,
		ListFilter,
		X,
	} from '@lucide/svelte';

	let { pollms = 5000 }: { pollms?: number } = $props();
	const poll = new RayPoll<RayClusterPayload>((f) => rayCluster(f));
	$effect(() => poll.start(pollms));

	/** The scaffold's lesson applied to the two reads it does not own: a dead upstream HANGS, so
	 *  every fetch here is armed with a timeout too. */
	const armed =
		(ms: number): typeof fetch =>
		(url, init) =>
			fetch(url, { ...init, signal: timeoutSignal(ms) });
	const msg = (e: unknown) => (e instanceof Error ? e.message : String(e));

	const payload = $derived(poll.data);
	const nodes = $derived(poll.data?.nodes ?? []);

	let chosenNode = $state<string | null>(null);
	const activeNode = $derived(
		(chosenNode && nodes.find((n) => n.node_id === chosenNode)?.node_id) ||
			(nodes.find((n) => n.is_head) ?? nodes[0])?.node_id ||
			'',
	);

	let selected = $state('');
	let lines = $state(500);
	let refreshTick = $state(0);

	// view options — the page's, minus its URL-backed ones
	let follow = $state(false);
	let wrap = $state(false);
	let lineNumbers = $state(true);
	let onlyMatches = $state(false);
	let query = $state('');
	let fileQuery = $state('');

	let files = $state<Record<string, string[]>>({});
	let filesError = $state<string | null>(null);
	let content = $state('');
	let contentError = $state<string | null>(null);
	let loading = $state(false);

	let pre: HTMLElement | null = $state(null);
	let stick = $state(true); // auto-scroll only while parked at the bottom

	const short = (s: string | null | undefined) =>
		(s ?? '').replace(/^kuberay-ai-dev-cluster-/, '') || '';
	const nodeName = (n: RayNode) => short(n.hostname) || n.node_ip || n.node_id?.slice(0, 12) || '?';

	// File inventory for the active node — re-read when the node changes (the page's param query
	// re-keying, expressed as an effect).
	$effect(() => {
		const node = activeNode;
		if (!node) return;
		let live = true;
		void (async () => {
			try {
				const p = await rayLogFiles(node, armed(6000));
				if (!live) return;
				files = p.ok ? p.files : {};
				filesError = p.ok ? null : (p.error ?? 'failed to list logs');
			} catch (e) {
				if (live) {
					files = {};
					filesError = msg(e);
				}
			}
		})();
		return () => {
			live = false;
		};
	});

	// Tail content — re-read on node / file / line-count change and on the refresh button, and
	// POLLED while following: a growing log file emits no "it grew" event (the page's reason).
	$effect(() => {
		const node = activeNode;
		const file = selected;
		const n = lines;
		const following = follow;
		refreshTick; // an explicit refresh re-runs this effect
		if (!node || !file) {
			content = '';
			contentError = null;
			return;
		}
		let live = true;
		const read = async () => {
			loading = true;
			try {
				const p = await rayLogContent(node, file, n, armed(8000));
				if (!live) return;
				content = p.ok ? (p.text ?? '') : '';
				contentError = p.ok ? null : (p.error ?? 'failed to read log');
			} catch (e) {
				if (live) contentError = msg(e);
			} finally {
				if (live) loading = false;
			}
		};
		void read();
		if (!following)
			return () => {
				live = false;
			};
		// POLL REASON: follow mode tails the selected log file — new lines land while it is on
		// screen, and an element has no push channel; 2.5s matches the zone logviewer's own tail.
		const timer = setInterval(() => void read(), 2500);
		return () => {
			live = false;
			clearInterval(timer);
		};
	});

	// Stick to the bottom when parked there (tail behaviour).
	$effect(() => {
		content;
		if (pre && stick) pre.scrollTop = pre.scrollHeight;
	});

	const error = $derived(contentError ?? filesError);

	function selectNode(id: string) {
		chosenNode = id;
		selected = '';
	}

	function openFile(node: HTMLElement, name: string) {
		selected = name;
		stick = true;
		node.dispatchEvent(
			new CustomEvent(RASK_SELECT, {
				bubbles: true,
				composed: true,
				detail: {
					source: 'rask-compute-logs',
					kind: 'ray-log-file',
					id: `${activeNode}:${name}`,
					label: name,
				} satisfies SelectDetail,
			}),
		);
	}

	function onScroll() {
		if (pre) stick = pre.scrollHeight - pre.scrollTop - pre.clientHeight < 40;
	}

	async function copy() {
		try {
			await navigator.clipboard.writeText(content);
		} catch {
			/* clipboard may be blocked */
		}
	}
	function download() {
		const blob = new Blob([content], { type: 'text/plain' });
		const url = URL.createObjectURL(blob);
		const a = document.createElement('a');
		a.href = url;
		a.download = (selected.split('/').pop() || 'log') + '.txt';
		a.click();
		URL.revokeObjectURL(url);
	}

	const allLines = $derived(content ? content.split('\n') : []);
	const matchCount = $derived(
		query ? allLines.filter((l) => l.toLowerCase().includes(query.toLowerCase())).length : 0,
	);
	const view = $derived.by(() => {
		let ls = allLines.map((t, i) => ({ n: i + 1, t }));
		if (query && onlyMatches) {
			const q = query.toLowerCase();
			ls = ls.filter((l) => l.t.toLowerCase().includes(q));
		}
		return ls;
	});

	// Mirrored from routes/logviewer/+page.svelte — the same lines must wear the same colours here.
	function levelClass(t: string): string {
		if (/\b(ERROR|CRITICAL|FATAL)\b|Traceback|Exception|Error:/.test(t)) return 'text-destructive';
		if (/\bWARN(ING)?\b/.test(t)) return 'text-amber-600 dark:text-amber-400';
		if (/\bDEBUG\b/.test(t)) return 'text-muted-foreground/60';
		return '';
	}
	// Split a line around case-insensitive matches for <mark> highlighting.
	function segs(t: string): { s: string; hit: boolean; k: number }[] {
		if (!query) return [{ s: t, hit: false, k: 0 }];
		const out: { s: string; hit: boolean; k: number }[] = [];
		const lc = t.toLowerCase();
		const lq = query.toLowerCase();
		let i = 0;
		let k = 0;
		for (;;) {
			const j = lc.indexOf(lq, i);
			if (j < 0) {
				out.push({ s: t.slice(i), hit: false, k: k++ });
				break;
			}
			if (j > i) out.push({ s: t.slice(i, j), hit: false, k: k++ });
			out.push({ s: t.slice(j, j + query.length), hit: true, k: k++ });
			i = j + query.length;
		}
		return out;
	}

	const filteredFiles = $derived.by(() => {
		const q = fileQuery.toLowerCase();
		const out: [string, string[]][] = [];
		for (const [cat, list] of Object.entries(files)) {
			const fl = q ? list.filter((f) => f.toLowerCase().includes(q)) : list;
			if (fl.length) out.push([cat, fl]);
		}
		return out;
	});
</script>

<div class="bg-background flex h-full min-h-0 flex-col gap-2 p-3 text-sm">
	<p class="text-muted-foreground text-[11px]">mounted {poll.mountedAt} · poll #{poll.polls}</p>
	{#if poll.error !== null}
		<p class="text-destructive text-sm">Ray unreachable: {poll.error}</p>
	{:else if payload && !payload.ok}
		<Card class="border-amber-500/40 bg-amber-500/10 p-3 text-sm">
			Ray dashboard unreachable at <span class="font-mono">{payload.dashboard_url}</span>
			{#if payload.error}
				<div class="text-muted-foreground mt-1 text-xs">{payload.error}</div>
			{/if}
		</Card>
	{:else if nodes.length === 0}
		<p class="text-muted-foreground text-sm">No nodes reported.</p>
	{:else}
		<!-- toolbar -->
		<div class="flex flex-wrap items-center gap-2">
			<select
				class="border-input bg-background h-7 rounded-md border px-2 text-xs"
				value={activeNode}
				onchange={(e) => selectNode(e.currentTarget.value)}
			>
				{#each nodes as n (n.node_id)}
					<option value={n.node_id}>{nodeName(n)}{n.is_head ? ' (head)' : ''}</option>
				{/each}
			</select>
			<select
				class="border-input bg-background h-7 rounded-md border px-2 text-xs"
				bind:value={lines}
			>
				{#each [200, 500, 2000, 10000] as n (n)}<option value={n}>{n} lines</option>{/each}
			</select>

			<button
				class="inline-flex h-7 items-center gap-1 rounded-md border px-2 text-xs transition-colors {follow
					? 'border-emerald-500/50 bg-emerald-500/10 text-emerald-600 dark:text-emerald-400'
					: 'hover:bg-muted'}"
				onclick={() => (follow = !follow)}
				disabled={!selected}
			>
				{#if follow}<Pause class="h-3 w-3" />following{:else}<Play class="h-3 w-3" />follow{/if}
			</button>
			<button
				class="hover:bg-muted inline-flex h-7 items-center gap-1 rounded-md border px-2 text-xs disabled:opacity-50"
				onclick={() => (refreshTick += 1)}
				disabled={!selected || loading}
			>
				<RefreshCw class="h-3 w-3 {loading ? 'animate-spin' : ''}" />
			</button>

			<!-- search within content -->
			<div class="relative">
				<Search
					class="text-muted-foreground pointer-events-none absolute top-1/2 left-2 h-3 w-3 -translate-y-1/2"
				/>
				<input
					class="border-input bg-background focus-visible:ring-ring h-7 w-52 rounded-md border pr-6 pl-7 text-xs focus-visible:ring-1 focus-visible:outline-none"
					placeholder="search in log…"
					bind:value={query}
				/>
				{#if query}
					<button
						class="text-muted-foreground hover:text-foreground absolute top-1/2 right-1.5 -translate-y-1/2"
						onclick={() => (query = '')}
					>
						<X class="h-3 w-3" />
					</button>
				{/if}
			</div>
			{#if query}
				<span class="text-muted-foreground text-xs tabular-nums"
					>{matchCount} match{matchCount === 1 ? '' : 'es'}</span
				>
				<button
					class="inline-flex h-7 items-center gap-1 rounded-md border px-2 text-xs transition-colors {onlyMatches
						? 'border-primary bg-primary/10 text-primary'
						: 'hover:bg-muted'}"
					title="show only matching lines"
					onclick={() => (onlyMatches = !onlyMatches)}
				>
					<ListFilter class="h-3 w-3" />
				</button>
			{/if}

			<div class="ml-auto flex items-center gap-1">
				<button
					class="inline-flex h-7 items-center gap-1 rounded-md border px-2 text-xs transition-colors {wrap
						? 'border-primary bg-primary/10 text-primary'
						: 'hover:bg-muted'}"
					title="wrap lines"
					onclick={() => (wrap = !wrap)}
				>
					<TextWrap class="h-3 w-3" />
				</button>
				<button
					class="inline-flex h-7 items-center gap-1 rounded-md border px-2 text-xs transition-colors {lineNumbers
						? 'border-primary bg-primary/10 text-primary'
						: 'hover:bg-muted'}"
					title="line numbers"
					onclick={() => (lineNumbers = !lineNumbers)}
				>
					<Hash class="h-3 w-3" />
				</button>
				<button
					class="hover:bg-muted inline-flex h-7 items-center gap-1 rounded-md border px-2 text-xs disabled:opacity-50"
					title="copy"
					onclick={copy}
					disabled={!content}
				>
					<Copy class="h-3 w-3" />
				</button>
				<button
					class="hover:bg-muted inline-flex h-7 items-center gap-1 rounded-md border px-2 text-xs disabled:opacity-50"
					title="download"
					onclick={download}
					disabled={!content}
				>
					<Download class="h-3 w-3" />
				</button>
			</div>
		</div>

		{#if error}
			<Card class="border-destructive/40 bg-destructive/10 text-destructive p-3">{error}</Card>
		{/if}

		<div class="flex min-h-0 flex-1 gap-3">
			<!-- file sidebar -->
			<Card class="flex w-64 shrink-0 flex-col overflow-hidden p-0">
				<div class="border-b p-2">
					<input
						class="border-input bg-background focus-visible:ring-ring h-7 w-full rounded-md border px-2 text-xs focus-visible:ring-1 focus-visible:outline-none"
						placeholder="filter files…"
						bind:value={fileQuery}
					/>
				</div>
				<div class="flex-1 overflow-auto">
					{#each filteredFiles as [cat, list] (cat)}
						<div
							class="text-muted-foreground bg-muted/40 sticky top-0 flex justify-between px-3 py-1 text-[10px] font-medium tracking-wide uppercase"
						>
							<span>{cat}</span><span>{list.length}</span>
						</div>
						{#each list as f (f)}
							<button
								class="hover:bg-muted/60 block w-full truncate px-3 py-1 text-left font-mono text-[11px] {selected === f
									? 'bg-primary/10 text-primary'
									: ''}"
								title={f}
								onclick={(e) => openFile(e.currentTarget, f)}
							>
								{f}
							</button>
						{/each}
					{/each}
					{#if !filteredFiles.length}
						<div class="text-muted-foreground p-3 text-xs">No files.</div>
					{/if}
				</div>
			</Card>

			<!-- content -->
			<Card class="flex min-w-0 flex-1 flex-col overflow-hidden p-0">
				{#if selected}
					<div
						class="text-muted-foreground flex shrink-0 items-center gap-2 border-b px-3 py-1.5 font-mono text-[11px]"
					>
						<span class="truncate">{selected}</span>
						<span class="ml-auto tabular-nums"
							>{view.length}{onlyMatches && query ? `/${allLines.length}` : ''} lines</span
						>
						{#if follow}<span class="flex items-center gap-1 text-emerald-600 dark:text-emerald-400"><span
							class="h-1.5 w-1.5 animate-pulse rounded-full bg-emerald-500"
						></span>live</span>{/if}
					</div>
				{/if}
				{#if !selected}
					<div class="text-muted-foreground flex h-full items-center justify-center gap-2 p-6">
						<FileText class="h-5 w-5" /> Select a log file.
					</div>
				{:else}
					<div
						bind:this={pre}
						onscroll={onScroll}
						class="min-h-0 flex-1 overflow-auto py-1 font-mono text-[11px] leading-relaxed {wrap
							? ''
							: 'whitespace-nowrap'}"
					>
						{#each view as l (l.n)}
							<div class="hover:bg-muted/40 flex gap-2 px-2 {levelClass(l.t)}">
								{#if lineNumbers}<span
										class="text-muted-foreground/40 shrink-0 text-right select-none"
										style="width:4ch">{l.n}</span
									>{/if}
								<span class="{wrap ? 'break-all whitespace-pre-wrap' : ''} min-w-0">
									{#if query}{#each segs(l.t) as seg (seg.k)}{#if seg.hit}<mark
													class="text-foreground bg-amber-300/60 dark:bg-amber-500/40">{seg.s}</mark
												>{:else}{seg.s}{/if}{/each}{:else}{l.t || ' '}{/if}
								</span>
							</div>
						{/each}
						{#if !view.length}<div class="text-muted-foreground p-3">
								{loading ? 'loading…' : '(empty)'}
							</div>{/if}
					</div>
				{/if}
			</Card>
		</div>
	{/if}
</div>
