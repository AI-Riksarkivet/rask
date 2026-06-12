<script lang="ts">
	import { page as pageStore } from '$app/state';
	import { goto } from '$app/navigation';
	import { Button } from '$lib/components/ui/button';
	import { Badge } from '$lib/components/ui/badge';
	import { toggleMode } from 'mode-watcher';
	import {
		Sun,
		Moon,
		LayoutGrid,
		ListTree,
		Server,
		ServerCog,
		Boxes,
		Image as ImageIcon,
		ScrollText,
		BookOpen,
		Search,
		Library,
	} from 'lucide-svelte';
	import { onMount, onDestroy, type Snippet } from 'svelte';
	import { rayHealth } from '$lib/api';

	interface Props {
		title?: string;
		center?: Snippet;
		actions?: Snippet;
		rightActions?: Snippet;
		/** Hide the inner top bar entirely (rare — used when a page wants to draw its own). */
		hideTopBar?: boolean;
		/** Drop the outer page padding/margins so the content can fill (e.g., the canvas viewer). */
		flush?: boolean;
		children: Snippet;
	}
	let {
		title,
		center,
		actions,
		rightActions,
		hideTopBar = false,
		flush = false,
		children,
	}: Props = $props();

	type NavItem = {
		href: string;
		label: string;
		icon: typeof LayoutGrid;
		match: (p: string) => boolean;
		/** Optional override; called instead of plain goto(href). Return false to fall back to href. */
		click?: () => Promise<boolean | void>;
	};
	const nav: NavItem[] = [
		{ href: '/batches', label: 'Batches', icon: LayoutGrid, match: (p) => p === '/batches' },
		{ href: '/search', label: 'Search', icon: Search, match: (p) => p.startsWith('/search') },
		{ href: '/browse', label: 'Browse', icon: Library, match: (p) => p.startsWith('/browse') },
		{ href: '/jobs', label: 'Jobs', icon: ListTree, match: (p) => p.startsWith('/jobs') },
		{ href: '/cluster', label: 'Cluster', icon: Server, match: (p) => p.startsWith('/cluster') },
		{ href: '/serve', label: 'Serve', icon: ServerCog, match: (p) => p.startsWith('/serve') },
		{ href: '/actors', label: 'Actors', icon: Boxes, match: (p) => p.startsWith('/actors') },
		{ href: '/embed', label: 'Ray', icon: ScrollText, match: (p) => p.startsWith('/embed') },
		{ href: '/api-docs', label: 'API', icon: BookOpen, match: (p) => p.startsWith('/api-docs') },
		{
			href: '/batches',
			label: 'Viewer',
			icon: ImageIcon,
			match: (p) => p.startsWith('/viewer'),
			click: async () => {
				// Random completed batch — falls back to /batches if none are done yet.
				try {
					const r = await fetch('/api/batches/random?status=done');
					if (!r.ok) return false;
					const { batch_id } = await r.json();
					await goto(`/viewer/${encodeURIComponent(batch_id)}`);
					return true;
				} catch {
					return false;
				}
			},
		},
	];

	async function handleNavClick(item: NavItem): Promise<void> {
		if (item.click) {
			const handled = await item.click();
			if (handled) return;
		}
		goto(item.href);
	}
	const path = $derived(pageStore.url?.pathname ?? '');

	let health = $state<{ ok: boolean; version?: string; error?: string } | null>(null);
	let timer: ReturnType<typeof setInterval> | null = null;

	async function refresh() {
		try {
			const r = await rayHealth();
			health = { ok: r.ok, version: r.ray_version, error: r.error };
		} catch (e) {
			health = { ok: false, error: e instanceof Error ? e.message : String(e) };
		}
	}

	onMount(() => {
		refresh();
		timer = setInterval(refresh, 5000);
	});
	onDestroy(() => {
		if (timer) clearInterval(timer);
	});
</script>

<div class="flex h-full w-full flex-1 flex-col overflow-hidden">
	<!-- Top bar -->
	{#if !hideTopBar}
		<header
			class="flex h-12 shrink-0 items-center gap-3 border-b bg-[oklch(0.18_0.015_260)] px-3 text-[oklch(0.95_0.005_260)] dark:bg-[oklch(0.13_0.01_260)]"
		>
			<button
				class="flex items-center gap-2 rounded px-2 py-1 text-sm font-semibold tracking-tight hover:bg-white/5"
				onclick={() => goto('/batches')}
				aria-label="Home"
			>
				<svg
					viewBox="0 0 24 24"
					class="h-5 w-5 text-[oklch(0.78_0.18_220)]"
					fill="none"
					stroke="currentColor"
					stroke-width="2.2"
					stroke-linecap="round"
					stroke-linejoin="round"
				>
					<path d="M5 6l7 6-7 6" />
					<path d="M13 6l7 6-7 6" />
				</svg>
				<span>RASK</span>
				<span class="text-[oklch(0.6_0.01_260)]">/</span>
				<span class="truncate text-[oklch(0.85_0.005_260)]">{title ?? path}</span>
			</button>

			{#if center}
				<div class="flex min-w-0 items-center gap-2 text-[oklch(0.85_0.005_260)]">
					{@render center()}
				</div>
			{/if}

			<div class="ml-auto flex items-center gap-1">
				{#if actions}
					{@render actions()}
				{/if}

				{#if health}
					{#if health.ok}
						<Badge variant="success" class="font-mono">
							<span class="mr-1 inline-block h-1.5 w-1.5 rounded-full bg-current"></span>
							ray {health.version ?? 'ok'}
						</Badge>
					{:else}
						<Badge
							variant="outline"
							class="border-white/10 bg-white/5 font-mono text-[oklch(0.85_0.005_260)]"
							title={health.error ?? ''}
						>
							<span class="mr-1 inline-block h-1.5 w-1.5 rounded-full bg-amber-400"></span>
							ray offline
						</Badge>
					{/if}
				{/if}

				<Button
					variant="ghost"
					size="icon-sm"
					class="text-[oklch(0.85_0.005_260)] hover:bg-white/10 hover:text-white"
					onclick={toggleMode}
					title="Toggle theme"
				>
					<Sun class="h-4 w-4 dark:hidden" />
					<Moon class="hidden h-4 w-4 dark:block" />
				</Button>
				{#if rightActions}
					{@render rightActions()}
				{/if}
			</div>
		</header>
	{/if}

	<div class="flex flex-1 overflow-hidden">
		<!-- Left rail -->
		<nav
			class="flex w-14 shrink-0 flex-col items-stretch gap-1 border-r bg-[oklch(0.965_0.004_260)] py-3 dark:bg-[oklch(0.16_0.008_260)]"
		>
			{#each nav as item (item.label)}
				{@const active = item.match(path)}
				<button
					type="button"
					class={`relative mx-1 flex flex-col items-center gap-0.5 rounded-md px-1 py-2 text-[10px] font-medium tracking-tight transition
						${
							active
								? 'bg-primary/10 text-primary dark:bg-primary/15'
								: 'text-muted-foreground hover:bg-accent hover:text-accent-foreground'
						}`}
					onclick={() => handleNavClick(item)}
					title={item.label}
				>
					{#if active}
						<span class="bg-primary absolute top-1.5 left-0 h-[calc(100%-12px)] w-[3px] rounded-r"
						></span>
					{/if}
					<item.icon class="h-5 w-5" />
					<span>{item.label}</span>
				</button>
			{/each}
		</nav>

		<main
			class={flush
				? 'bg-background flex flex-1 overflow-hidden'
				: 'bg-background flex-1 overflow-auto'}
		>
			{@render children()}
		</main>
	</div>
</div>
