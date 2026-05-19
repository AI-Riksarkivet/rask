<script lang="ts">
	import { enhance } from '$app/forms';
	import { ArrowLeft, Server } from 'lucide-svelte';
	import { Button } from '$lib/components/ui/button/index.js';
	import { Input } from '$lib/components/ui/input/index.js';
	import { Label } from '$lib/components/ui/label/index.js';
	import RetroGrid from '$lib/components/ui/magic/retro-grid/retro-grid.svelte';

	let {
		form,
		data,
	}: {
		form: { error?: string } | null;
		data: { hasExistingSession: boolean; prefillTenant?: string };
	} = $props();
	let loading = $state(false);
</script>

<svelte:head>
	<title>Login - RA HCP Console</title>
</svelte:head>

<div class="relative flex min-h-screen items-center justify-center bg-background px-4">
	<RetroGrid />
	<div class="relative z-10 w-full max-w-sm">
		<div class="mb-8 text-center">
			<div class="mx-auto mb-4 flex h-14 w-14 items-center justify-center rounded-xl bg-primary/10">
				<Server class="h-7 w-7 text-primary" />
			</div>
			<h1 class="text-2xl font-bold">RA HCP Console</h1>
			<p class="mt-1 text-sm text-muted-foreground">
				{#if data.hasExistingSession}
					Add another tenant session
				{:else}
					Sign in to your HCP tenant
				{/if}
			</p>
		</div>

		{#if form?.error}
			<div
				class="mb-4 rounded-lg border border-destructive/20 bg-destructive/10 p-3 text-sm text-destructive"
			>
				{form.error}
			</div>
		{/if}

		<form
			method="POST"
			use:enhance={() => {
				loading = true;
				return async ({ update }) => {
					loading = false;
					await update();
				};
			}}
			class="rounded-xl border bg-card p-6 shadow-sm"
		>
			<div class="space-y-4">
				<div class="space-y-2">
					<Label for="tenant">Tenant</Label>
					<Input
						id="tenant"
						name="tenant"
						placeholder="Tenant name (optional)"
						value={data.prefillTenant ?? ''}
					/>
				</div>
				<div class="space-y-2">
					<Label for="username">Username</Label>
					<Input id="username" name="username" placeholder="Enter your HCP username" required />
				</div>
				<div class="space-y-2">
					<Label for="password">Password</Label>
					<Input
						id="password"
						type="password"
						name="password"
						placeholder="Enter your password"
						required
					/>
				</div>
			</div>
			<div class="mt-6 flex flex-col gap-3">
				<Button type="submit" class="w-full" disabled={loading}>
					{#if loading}
						Signing in...
					{:else}
						Sign in
					{/if}
				</Button>
				{#if data.hasExistingSession}
					<Button variant="ghost" class="w-full gap-2" href="/namespaces">
						<ArrowLeft class="h-4 w-4" />
						Back to current session
					</Button>
				{/if}
			</div>
		</form>
	</div>
</div>
