<script lang="ts">
	import { onMount } from 'svelte';
	import { page } from '$app/state';
	import { rayCluster, rayLogFiles, rayLogContent, type RayNode } from '$lib/api';
	import RayShell from '$lib/components/layout/ray-shell.svelte';
	import { Card } from '$lib/components/ui/card';
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
		X
	} from 'lucide-svelte';

	let nodes = $state<RayNode[]>([]);
	let nodeId = $state<string>('');
	let files = $state<Record<string, string[]>>({});
	let selected = $state<string>('');
	let content = $state<string>('');
	let lines = $state(500);
	let error = $state<string | null>(null);
	let loading = $state(false);

	// view options
	let follow = $state(false);
	let wrap = $state(false);
	let lineNumbers = $state(true);
	let onlyMatches = $state(false);
	let query = $state('');
	let fileQuery = $state('');

	let pre: HTMLElement | null = $state(null);
	let stick = $state(true); // auto-scroll only while parked at the bottom

	const short = (s: string | null | undefined) =>
		(s ?? '').replace(/^kuberay-ai-dev-cluster-/, '') || '';
	const nodeName = (n: RayNode) => short(n.hostname) || n.node_ip || n.node_id?.slice(0, 12) || '?';

	async function loadNodes() {
		try {
			nodes = (await rayCluster()).nodes ?? [];
			const qNode = page.url.searchParams.get('node');
			nodeId =
				(qNode && nodes.find((n) => n.node_id === qNode)?.node_id) ||
				(nodes.find((n) => n.is_head) ?? nodes[0])?.node_id ||
				'';
			fileQuery = page.url.searchParams.get('q') ?? '';
			await loadFiles();
			const qFile = page.url.searchParams.get('file');
			if (qFile) await openFile(qFile);
		} catch (e) {
			error = e instanceof Error ? e.message : String(e);
		}
	}

	async function loadFiles() {
		if (!nodeId) return;
		selected = '';
		content = '';
		try {
			const p = await rayLogFiles(nodeId);
			files = p.ok ? p.files : {};
			error = p.ok ? null : (p.error ?? 'failed to list logs');
		} catch (e) {
			error = e instanceof Error ? e.message : String(e);
		}
	}

	async function openFile(name: string) {
		selected = name;
		stick = true;
		await loadContent();
	}

	async function loadContent() {
		if (!nodeId || !selected) return;
		loading = true;
		try {
			const p = await rayLogContent(nodeId, selected, lines);
			content = p.ok ? (p.text ?? '') : '';
			error = p.ok ? null : (p.error ?? 'failed to read log');
		} catch (e) {
			error = e instanceof Error ? e.message : String(e);
		} finally {
			loading = false;
		}
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

	onMount(loadNodes);

	// Live tail.
	$effect(() => {
		if (!follow || !selected) return;
		const t = setInterval(loadContent, 2500);
		return () => clearInterval(t);
	});

	// Stick to the bottom when parked there (tail behaviour).
	$effect(() => {
		content;
		if (pre && stick) pre.scrollTop = pre.scrollHeight;
	});

	const allLines = $derived(content ? content.split('\n') : []);
	const matchCount = $derived(
		query ? allLines.filter((l) => l.toLowerCase().includes(query.toLowerCase())).length : 0
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

<RayShell title="Logs">
	<div class="flex h-full flex-col gap-3 p-4 text-sm">
		<!-- toolbar -->
		<div class="flex flex-wrap items-center gap-2">
			<select class="border-input bg-background h-7 rounded-md border px-2 text-xs" bind:value={nodeId} onchange={loadFiles}>
				{#each nodes as n (n.node_id)}
					<option value={n.node_id}>{nodeName(n)}{n.is_head ? ' (head)' : ''}</option>
				{/each}
			</select>
			<select class="border-input bg-background h-7 rounded-md border px-2 text-xs" bind:value={lines} onchange={loadContent}>
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
			<button class="hover:bg-muted inline-flex h-7 items-center gap-1 rounded-md border px-2 text-xs disabled:opacity-50" onclick={loadContent} disabled={!selected || loading}>
				<RefreshCw class="h-3 w-3 {loading ? 'animate-spin' : ''}" />
			</button>

			<!-- search within content -->
			<div class="relative">
				<Search class="text-muted-foreground pointer-events-none absolute top-1/2 left-2 h-3 w-3 -translate-y-1/2" />
				<input
					class="border-input bg-background focus-visible:ring-ring h-7 w-52 rounded-md border pr-6 pl-7 text-xs focus-visible:ring-1 focus-visible:outline-none"
					placeholder="search in log…"
					bind:value={query}
				/>
				{#if query}
					<button class="text-muted-foreground hover:text-foreground absolute top-1/2 right-1.5 -translate-y-1/2" onclick={() => (query = '')}>
						<X class="h-3 w-3" />
					</button>
				{/if}
			</div>
			{#if query}
				<span class="text-muted-foreground text-xs tabular-nums">{matchCount} match{matchCount === 1 ? '' : 'es'}</span>
				<button
					class="inline-flex h-7 items-center gap-1 rounded-md border px-2 text-xs transition-colors {onlyMatches ? 'border-primary bg-primary/10 text-primary' : 'hover:bg-muted'}"
					title="show only matching lines"
					onclick={() => (onlyMatches = !onlyMatches)}
				>
					<ListFilter class="h-3 w-3" />
				</button>
			{/if}

			<div class="ml-auto flex items-center gap-1">
				<button class="inline-flex h-7 items-center gap-1 rounded-md border px-2 text-xs transition-colors {wrap ? 'border-primary bg-primary/10 text-primary' : 'hover:bg-muted'}" title="wrap lines" onclick={() => (wrap = !wrap)}>
					<TextWrap class="h-3 w-3" />
				</button>
				<button class="inline-flex h-7 items-center gap-1 rounded-md border px-2 text-xs transition-colors {lineNumbers ? 'border-primary bg-primary/10 text-primary' : 'hover:bg-muted'}" title="line numbers" onclick={() => (lineNumbers = !lineNumbers)}>
					<Hash class="h-3 w-3" />
				</button>
				<button class="hover:bg-muted inline-flex h-7 items-center gap-1 rounded-md border px-2 text-xs disabled:opacity-50" title="copy" onclick={copy} disabled={!content}>
					<Copy class="h-3 w-3" />
				</button>
				<button class="hover:bg-muted inline-flex h-7 items-center gap-1 rounded-md border px-2 text-xs disabled:opacity-50" title="download" onclick={download} disabled={!content}>
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
						<div class="text-muted-foreground bg-muted/40 sticky top-0 flex justify-between px-3 py-1 text-[10px] font-medium tracking-wide uppercase">
							<span>{cat}</span><span>{list.length}</span>
						</div>
						{#each list as f (f)}
							<button
								class="hover:bg-muted/60 block w-full truncate px-3 py-1 text-left font-mono text-[11px] {selected === f ? 'bg-primary/10 text-primary' : ''}"
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
					<div class="text-muted-foreground flex shrink-0 items-center gap-2 border-b px-3 py-1.5 font-mono text-[11px]">
						<span class="truncate">{selected}</span>
						<span class="ml-auto tabular-nums">{view.length}{onlyMatches && query ? `/${allLines.length}` : ''} lines</span>
						{#if follow}<span class="flex items-center gap-1 text-emerald-600 dark:text-emerald-400"><span class="h-1.5 w-1.5 animate-pulse rounded-full bg-emerald-500"></span>live</span>{/if}
					</div>
				{/if}
				{#if !selected}
					<div class="text-muted-foreground flex h-full items-center justify-center gap-2 p-6">
						<FileText class="h-5 w-5" /> Select a log file.
					</div>
				{:else}
					<div bind:this={pre} onscroll={onScroll} class="min-h-0 flex-1 overflow-auto py-1 font-mono text-[11px] leading-relaxed {wrap ? '' : 'whitespace-nowrap'}">
						{#each view as l (l.n)}
							<div class="hover:bg-muted/40 flex gap-2 px-2 {levelClass(l.t)}">
								{#if lineNumbers}<span class="text-muted-foreground/40 shrink-0 text-right select-none" style="width:4ch">{l.n}</span>{/if}
								<span class="{wrap ? 'break-all whitespace-pre-wrap' : ''} min-w-0">
									{#if query}{#each segs(l.t) as seg (seg.k)}{#if seg.hit}<mark class="text-foreground bg-amber-300/60 dark:bg-amber-500/40">{seg.s}</mark>{:else}{seg.s}{/if}{/each}{:else}{l.t || ' '}{/if}
								</span>
							</div>
						{/each}
						{#if !view.length}<div class="text-muted-foreground p-3">{loading ? 'loading…' : '(empty)'}</div>{/if}
					</div>
				{/if}
			</Card>
		</div>
	</div>
</RayShell>
