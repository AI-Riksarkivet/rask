<script lang="ts">
	import { Avatar, AvatarFallback } from '../components/avatar/index.js';
	import * as DropdownMenu from '../components/dropdown-menu/index.js';
	import { Button } from '../components/button/index.js';
	import { toggleMode } from 'mode-watcher';
	import { Sun, Moon, LogIn, LogOut } from '@lucide/svelte';
	import type { NavUser } from './nav-config.js';

	// The shell's identity and theme control, at the right of the top row: an avatar trigger and its
	// dropdown (identity, theme toggle, the auth control). Context-free (no sidebar
	// dependency) so it renders on every page. The
	// identity flows in as a plain prop from the app's root layout load (single-sourced via
	// the console's sessionToUser) — the shared shell never imports app data.
	//
	// `authEnabled` drives the auth affordance: off → the local identity,
	// no auth UI; on + signed in → identity + Sign out; on + signed out (user null) → a "Sign in" prompt.
	// Login/logout are the app's own `/auth/*` pages: the session lives in the page's memory, so they are
	// soft navigations — a document load would drop it.
	let {
		user = null,
		authEnabled = false,
		pathname = '',
	}: { user?: NavUser | null; authEnabled?: boolean; pathname?: string } = $props();

	const signedIn = $derived(!!user);
	// The identity shown on the trigger. NEVER invented: in a governance UI the point is knowing who
	// you are, so a signed-out reader is shown as signed out in both auth modes; only the cue differs
	// (a live sign-in link, or a statement that this stack signs nobody in).
	const identity = $derived(
		user ??
			(authEnabled
				? { name: 'Sign in', email: 'not signed in', initials: '?' }
				: { name: 'Not signed in', email: 'auth disabled on this stack', initials: '—' }),
	);
	// Return to the app the user signed in from (validated as a local path in the login route).
	const loginHref = $derived(`/auth/login?redirect=${encodeURIComponent(pathname || '/')}`);
</script>

<DropdownMenu.Root>
	<DropdownMenu.Trigger>
		{#snippet child({ props })}
			<Button {...props} variant="ghost" size="icon" class="rounded-full" aria-label="Account">
				<Avatar class="size-7 rounded-full">
					<AvatarFallback class="rounded-full text-xs">{identity.initials ?? '—'}</AvatarFallback>
				</Avatar>
			</Button>
		{/snippet}
	</DropdownMenu.Trigger>
	<DropdownMenu.Content class="min-w-56 rounded-lg" side="bottom" align="end" sideOffset={4}>
		<DropdownMenu.Label class="p-0 font-normal">
			<div class="flex items-center gap-2 px-1 py-1.5 text-left text-sm">
				<Avatar class="size-8 rounded-lg">
					<AvatarFallback class="rounded-lg">{identity.initials ?? '—'}</AvatarFallback>
				</Avatar>
				<div class="grid flex-1 text-left text-sm leading-tight">
					<span class="truncate font-medium">{identity.name}</span>
					<span class="truncate text-xs">{identity.email ?? 'local'}</span>
				</div>
			</div>
		</DropdownMenu.Label>
		<DropdownMenu.Separator />
		<DropdownMenu.Item onclick={toggleMode}>
			<Sun class="size-4 dark:hidden" />
			<Moon class="hidden size-4 dark:block" />
			Toggle theme
		</DropdownMenu.Item>
		{#if authEnabled}
			<DropdownMenu.Separator />
			{#if signedIn}
				<!-- The shadcn Item wrapper omits bits-ui's `child`, so the anchor is the item's content and
				     fills the row. -->
				<DropdownMenu.Item>
					<a href="/auth/logout" class="flex w-full items-center gap-2">
						<LogOut class="size-4" />
						Sign out
					</a>
				</DropdownMenu.Item>
			{:else}
				<!-- Returns here after the round-trip. -->
				<DropdownMenu.Item>
					<a href={loginHref} class="flex w-full items-center gap-2">
						<LogIn class="size-4" />
						Sign in
					</a>
				</DropdownMenu.Item>
			{/if}
		{/if}
	</DropdownMenu.Content>
</DropdownMenu.Root>
