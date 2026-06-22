<script lang="ts">
	import { page as pageStore } from '$app/state';
	import { Button } from '@rask/ui/button';
	import { Badge } from '@rask/ui/badge';
	import * as Sidebar from '@rask/ui/sidebar';
	import { toggleMode } from 'mode-watcher';
	import { Sun, Moon } from 'lucide-svelte';
	import { onMount, onDestroy, type Snippet } from 'svelte';
	import { rayHealth } from '@rask/api';

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
	<!-- Top bar — the unified sidebar (in +layout.svelte) owns navigation; this is
	     the per-page header: trigger, breadcrumb, page snippets, ray health, theme. -->
	{#if !hideTopBar}
		<header
			class="bg-sidebar text-sidebar-foreground border-sidebar-border flex h-12 shrink-0 items-center gap-3 border-b px-3"
		>
			<Sidebar.Trigger
				class="text-sidebar-foreground/80 hover:bg-sidebar-accent hover:text-sidebar-accent-foreground"
			/>

			<div class="flex items-center gap-2 text-sm font-semibold tracking-tight">
				<span class="text-sidebar-foreground/40">/</span>
				<span class="text-sidebar-foreground truncate">{title ?? path}</span>
			</div>

			{#if center}
				<div class="text-sidebar-foreground flex min-w-0 items-center gap-2">
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
							class="border-sidebar-border bg-sidebar-accent/40 text-sidebar-foreground font-mono"
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
					class="text-sidebar-foreground/80 hover:bg-sidebar-accent hover:text-sidebar-accent-foreground"
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

	<main
		class={flush
			? 'bg-background flex flex-1 overflow-hidden'
			: 'bg-background flex-1 overflow-auto'}
	>
		{@render children()}
	</main>
</div>
