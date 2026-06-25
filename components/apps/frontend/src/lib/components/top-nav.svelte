<script lang="ts">
	import { base } from '$app/paths';
	import { Button } from '@rask/ui/button';
	import * as DropdownMenu from '@rask/ui/dropdown-menu';
	import * as Avatar from '@rask/ui/avatar';
	import type { NavUser } from '@rask/ui/shell';
	import { mode, toggleMode } from 'mode-watcher';
	import { Boxes, ChevronsUpDown, Plus, Sun, Moon, Settings, LogIn } from '@lucide/svelte';

	// App-local platform top navbar — rendered ONLY by the catch-all host at `/` (the
	// pre-project home/picker, which has no sidebar). It COMPOSES @rask/ui primitives
	// (Button / DropdownMenu / Avatar); it is not a restyled duplicate of one. The six
	// domain MFEs render the @rask/ui AppShell sidebar instead, never this bar.

	// Plain const — the project list never mutates, so it is not $state. Same shape the
	// catch-all +page.svelte picker uses, so the navbar and the picker stay in sync.
	const projects = [{ name: 'Default', slug: 'default', subtitle: 'The default rask workspace' }];

	// No auth yet (deferred) — a local placeholder identity typed by the shell's NavUser.
	const user = { name: 'Guest', email: 'No account', initials: 'G' } satisfies NavUser;
</script>

<header class="bg-background sticky top-0 z-40 flex h-14 items-center gap-2 border-b px-4">
	<!-- BRAND: in-app soft nav home link. base === '' on the catch-all → resolves to '/'. -->
	<a href={base} class="hover:text-foreground flex items-center gap-2 transition-colors">
		<Boxes class="text-primary size-5" />
		<span class="font-semibold tracking-tight">rask</span>
	</a>

	<div class="ml-auto flex items-center gap-1">
		<!-- PROJECTS: compact switcher. Each entry is a CROSS-ZONE link into overview-frontend,
		     so it is project-slug-prefixed (NOT base-prefixed) and carries data-sveltekit-reload. -->
		<DropdownMenu.Root>
			<DropdownMenu.Trigger>
				{#snippet child({ props })}
					<Button {...props} variant="ghost" size="sm">
						Projects
						<ChevronsUpDown class="text-muted-foreground" />
					</Button>
				{/snippet}
			</DropdownMenu.Trigger>
			<DropdownMenu.Content align="end" class="min-w-56">
				{#each projects as p (p.slug)}
					<DropdownMenu.Item class="p-0">
						<a
							href="/{p.slug}/overview"
							data-sveltekit-reload
							class="flex w-full flex-col gap-0.5 px-2 py-1.5"
						>
							<span class="font-medium">{p.name}</span>
							<span class="text-muted-foreground text-xs">{p.subtitle}</span>
						</a>
					</DropdownMenu.Item>
				{/each}
				<DropdownMenu.Separator />
				<DropdownMenu.Item disabled>
					<Plus class="size-4" />
					New project (soon)
				</DropdownMenu.Item>
			</DropdownMenu.Content>
		</DropdownMenu.Root>

		<!-- THEME TOGGLE: toggleMode only touches the DOM inside the handler (SSR-safe).
		     mode.current is a reactive getter — read it directly, no $state/$effect mirror. -->
		<Button variant="ghost" size="icon" aria-label="Toggle theme" onclick={toggleMode}>
			{#if mode.current === 'dark'}
				<Sun />
			{:else}
				<Moon />
			{/if}
		</Button>

		<!-- USER / SETTINGS: placeholder identity (no auth). Avatar fallback only, no image. -->
		<DropdownMenu.Root>
			<DropdownMenu.Trigger>
				{#snippet child({ props })}
					<button {...props} class="rounded-md outline-none" aria-label="Open user menu">
						<Avatar.Root class="size-8">
							<Avatar.Fallback>{user.initials}</Avatar.Fallback>
						</Avatar.Root>
					</button>
				{/snippet}
			</DropdownMenu.Trigger>
			<DropdownMenu.Content align="end" class="min-w-56">
				<DropdownMenu.Label class="flex flex-col gap-0.5">
					<span class="font-medium">{user.name}</span>
					<span class="text-muted-foreground text-xs">{user.email}</span>
				</DropdownMenu.Label>
				<DropdownMenu.Separator />
				<DropdownMenu.Item disabled>
					<Settings class="size-4" />
					Settings (soon)
				</DropdownMenu.Item>
				<DropdownMenu.Item onclick={toggleMode}>
					{#if mode.current === 'dark'}
						<Sun class="size-4" />
						Light mode
					{:else}
						<Moon class="size-4" />
						Dark mode
					{/if}
				</DropdownMenu.Item>
				<DropdownMenu.Separator />
				<DropdownMenu.Item disabled>
					<LogIn class="size-4" />
					Sign in (soon)
				</DropdownMenu.Item>
			</DropdownMenu.Content>
		</DropdownMenu.Root>
	</div>
</header>
