<script lang="ts">
	import { onMount } from 'svelte';
	import { page } from '$app/state';
	import { type RayNode } from '@rask/api';
	import { getRayCluster, getLogFiles, getLogContent } from '$lib/remote/compute.remote';
	import { Card } from '@rask/ui/card';
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

	// THE ONE PATTERN (see lib/remote/compute.remote.ts): every read is a cached
	// remote query threading `getRequestEvent().fetch`. Nodes come from the cluster
	// query; files + content are PARAM queries re-keyed by the current node /
	// filename / line count, so changing any of them re-runs the read. Selection is
	// the only mutable local state; everything displayed derives from `.current`.
	const clusterQuery = getRayCluster();
	const nodes = $derived(clusterQuery.current?.nodes ?? []);

	// Deep-link defaults from the URL, resolved once nodes have loaded.
	const qNode = $derived(page.url.searchParams.get('node'));
	const nodeId = $derived(
		(qNode && nodes.find((n) => n.node_id === qNode)?.node_id) ||
			(nodes.find((n) => n.is_head) ?? nodes[0])?.node_id ||
			'',
	);

	let selectedNode = $state<string | null>(null);
	let selected = $state<string>('');
	let lines = $state(500);
	const activeNode = $derived(selectedNode ?? nodeId);

	// view options
	let follow = $state(false);
	let wrap = $state(false);
	let lineNumbers = $state(true);
	let onlyMatches = $state(false);
	let query = $state('');
	let fileQuery = $state(page.url.searchParams.get('q') ?? '');

	let pre: HTMLElement | null = $state(null);
	let stick = $state(true); // auto-scroll only while parked at the bottom

	const short = (s: string | null | undefined) =>
		(s ?? '').replace(/^kuberay-ai-dev-cluster-/, '') || '';
	const nodeName = (n: RayNode) => short(n.hostname) || n.node_ip || n.node_id?.slice(0, 12) || '?';

	// File inventory for the active node — param query, re-keys on node change.
	const filesQuery = $derived(activeNode ? getLogFiles({ nodeId: activeNode }) : null);
	const filesPayload = $derived(filesQuery?.current ?? null);
	const files = $derived(filesPayload?.ok ? filesPayload.files : {});

	// Tail content for the selected file — param query, re-keys on node/file/lines.
	const contentQuery = $derived(
		activeNode && selected
			? getLogContent({ nodeId: activeNode, filename: selected, lines })
			: null,
	);
	const contentPayload = $derived(contentQuery?.current ?? null);
	const content = $derived(contentPayload?.ok ? (contentPayload.text ?? '') : '');
	const loading = $derived(contentQuery?.loading ?? false);
	const error = $derived.by(() => {
		if (filesQuery?.error) return String(filesQuery.error);
		if (contentQuery?.error) return String(contentQuery.error);
		if (filesPayload && !filesPayload.ok) return filesPayload.error ?? 'failed to list logs';
		if (contentPayload && !contentPayload.ok) return contentPayload.error ?? 'failed to read log';
		return null;
	});

	// POLL REASON: the Ray dashboard REST API is snapshot-only introspection — Ray publishes no
	// change events a cursor could ride, so the node list re-reads on a clock (the file/content
	// reads re-key themselves; the manual refresh button re-fetches the active content query),
	// and the live tail below polls because a growing log file emits no "it grew" event.
	onMount(() => {
		const timer = setInterval(() => clusterQuery.refresh().catch(() => {}), 5000);
		return () => clearInterval(timer);
	});

	function selectNode(id: string) {
		selectedNode = id;
		selected = '';
	}

	function openFile(name: string) {
		selected = name;
		stick = true;
	}

	function refreshContent() {
		contentQuery?.refresh().catch(() => {});
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

	// Open a deep-linked file once its node's inventory has loaded.
	let deepLinkOpened = false;
	$effect(() => {
		if (deepLinkOpened || !filesPayload?.ok) return;
		const qFile = page.url.searchParams.get('file');
		if (qFile) openFile(qFile);
		deepLinkOpened = true;
	});

	// Live tail — poll the content query while following. `.refresh().catch()` is
	// mandatory: an uncaught refresh rejection evicts the query and kills the loop.
	$effect(() => {
		if (!follow || !selected) return;
		const cq = contentQuery;
		if (!cq) return;
		const t = setInterval(() => cq.refresh().catch(() => {}), 2500);
		return () => clearInterval(t);
	});

	// Stick to the bottom when parked there (tail behaviour).
	$effect(() => {
		content;
		if (pre && stick) pre.scrollTop = pre.scrollHeight;
	});

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

<svelte:head>
	<title>Logs — RASK</title>
</svelte:head>

<main class="bg-background flex-1 overflow-auto">
	<div class="flex h-full flex-col gap-3 p-4 text-sm">
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
				onclick={refreshContent}
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
								onclick={() => openFile(f)}
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
						{#if follow}<span
								class="flex items-center gap-1 text-emerald-600 dark:text-emerald-400"
							><span class="h-1.5 w-1.5 animate-pulse rounded-full bg-emerald-500"></span>live</span
							>{/if}
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
	</div>
</main>
