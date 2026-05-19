<script lang="ts">
	import { page } from '$app/state';
	import { mode, setMode } from 'mode-watcher';
	import { Sun, Moon, Monitor, User, Copy, Check, Shield } from 'lucide-svelte';
	import { toast } from 'svelte-sonner';
	import * as Card from '$lib/components/ui/card/index.js';
	import { Button } from '$lib/components/ui/button/index.js';
	import { Badge } from '$lib/components/ui/badge/index.js';
	import PageHeader from '$lib/components/custom/page-header/page-header.svelte';
	import { useCopyFeedback } from '$lib/utils/use-copy-feedback.svelte.js';

	let username = $derived(page.data.username as string);
	let tenant = $derived(page.data.tenant as string | undefined);
	let userGUID = $derived(page.data.userGUID as string | undefined);
	let roles = $derived((page.data.roles as string[]) ?? []);

	const { copied, copy } = useCopyFeedback();

	async function copyCanonicalId() {
		if (!userGUID) return;
		await copy(userGUID);
		toast.success('Canonical ID copied');
	}

	const themes = [
		{ value: 'light', label: 'Light', icon: Sun },
		{ value: 'dark', label: 'Dark', icon: Moon },
		{ value: 'system', label: 'System', icon: Monitor },
	] as const;
</script>

<svelte:head>
	<title>Settings - HCP Admin Console</title>
</svelte:head>

<div class="space-y-6">
	<PageHeader title="Settings" description="Manage your preferences and view your profile" />

	<div class="grid gap-6 md:grid-cols-2">
		<Card.Root>
			<Card.Header>
				<Card.Title>Theme Preference</Card.Title>
				<Card.Description>Choose how the console looks to you</Card.Description>
			</Card.Header>
			<Card.Content>
				<div class="flex gap-2">
					{#each themes as theme (theme.value)}
						<Button
							variant={mode.current === theme.value ? 'default' : 'outline'}
							size="sm"
							onclick={() => setMode(theme.value)}
						>
							<theme.icon class="mr-1.5 h-4 w-4" />
							{theme.label}
						</Button>
					{/each}
				</div>
			</Card.Content>
		</Card.Root>

		<Card.Root>
			<Card.Header>
				<Card.Title>User Profile</Card.Title>
				<Card.Description>Your account information</Card.Description>
			</Card.Header>
			<Card.Content class="space-y-4">
				<div class="flex items-center gap-3">
					<div
						class="flex h-10 w-10 items-center justify-center rounded-full bg-primary/10 text-primary"
					>
						<User class="h-5 w-5" />
					</div>
					<div>
						<p class="text-sm font-semibold">{username}</p>
						{#if tenant}
							<p class="text-xs text-muted-foreground">{tenant}</p>
						{/if}
					</div>
				</div>

				{#if roles.length > 0}
					<div class="space-y-1.5">
						<p class="flex items-center gap-1.5 text-xs font-medium text-muted-foreground">
							<Shield class="h-3.5 w-3.5" />
							Roles
						</p>
						<div class="flex flex-wrap gap-1.5">
							{#each roles as role (role)}
								<Badge variant="secondary">{role}</Badge>
							{/each}
						</div>
					</div>
				{/if}

				{#if userGUID}
					<div class="space-y-1.5">
						<p class="text-xs font-medium text-muted-foreground">Canonical ID</p>
						<button
							class="flex w-full cursor-pointer items-center gap-1.5 rounded-md bg-muted/60 px-2.5 py-1.5 transition-colors hover:bg-muted"
							onclick={copyCanonicalId}
							title="Click to copy canonical ID"
						>
							<span class="flex-1 truncate text-left font-mono text-xs text-muted-foreground">
								{userGUID}
							</span>
							{#if copied}
								<Check class="h-3.5 w-3.5 shrink-0 text-emerald-500" />
							{:else}
								<Copy class="h-3.5 w-3.5 shrink-0 text-muted-foreground" />
							{/if}
						</button>
					</div>
				{/if}
			</Card.Content>
		</Card.Root>
	</div>
</div>
