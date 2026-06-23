<script lang="ts">
	import { Boxes, Plus, ArrowRight } from '@lucide/svelte';

	// Home / project picker. Projects aren't in the backend yet, so this is a single
	// implicit "Default" workspace — wire it to real projects when that API lands.
	const projects = [
		{
			name: 'Default',
			subtitle: 'The default rask workspace',
			href: '/compute',
		},
	];
</script>

<div class="mx-auto w-full max-w-5xl p-6">
	<header class="mb-6">
		<h1 class="text-2xl font-semibold tracking-tight">Projects</h1>
		<p class="text-muted-foreground mt-1 text-sm">
			Open a project to work with its compute, documents, batches and storage.
		</p>
	</header>

	<div class="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
		{#each projects as p (p.name)}
			<a
				href={p.href}
				class="group bg-card hover:border-primary/50 flex flex-col rounded-xl border p-5 transition-colors"
			>
				<div
					class="bg-sidebar-primary text-sidebar-primary-foreground mb-3 flex size-10 items-center justify-center rounded-lg"
				>
					<Boxes class="size-5" />
				</div>
				<div class="font-medium">{p.name}</div>
				<div class="text-muted-foreground text-sm">{p.subtitle}</div>
				<div
					class="text-muted-foreground group-hover:text-foreground mt-4 flex items-center gap-1 text-sm"
				>
					Open <ArrowRight class="size-4" />
				</div>
			</a>
		{/each}

		<div
			class="border-border text-muted-foreground flex min-h-[164px] flex-col items-center justify-center gap-1 rounded-xl border border-dashed text-sm"
		>
			<Plus class="size-5" />
			New project (soon)
		</div>
	</div>
</div>
