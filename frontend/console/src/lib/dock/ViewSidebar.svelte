<script lang="ts">
	/**
	 * The saved-views sidebar — select a view, or create one from the current arrangement.
	 *
	 * The modified marker beside the active view is the visible half of the decision `DockViews` makes:
	 * loading a view does NOT redirect autosave at it. Autosave keeps writing the implicit draft, and
	 * the view stays frozen until you press Save. Overwriting on every drag would mean you could never
	 * open a saved arrangement to LOOK at it without destroying it.
	 */
	import { Check, Plus, Save, Trash2 } from '@lucide/svelte';
	import { Button } from '@riksarkivet/ui/button';
	import { Input } from '@riksarkivet/ui/input';
	import type { DockViews } from './views.svelte.ts';

	interface Props {
		views: DockViews;
		onselect: (id: string) => void;
	}
	let { views, onselect }: Props = $props();

	let creating = $state(false);
	let draftName = $state('');

	async function create(): Promise<void> {
		if (draftName.trim() === '') return;
		if (await views.saveAs(draftName)) {
			draftName = '';
			creating = false;
		}
	}
</script>

<aside class="views">
	<header>
		<span class="heading">Views</span>
		<Button
			variant="ghost"
			size="icon-xs"
			title="Save the current arrangement as a new view"
			aria-label="New view"
			onclick={() => (creating = !creating)}
		>
			<Plus size={14} />
		</Button>
	</header>

	{#if creating}
		<form
			class="new"
			onsubmit={(e) => {
				e.preventDefault();
				void create();
			}}
		>
			<!-- The field appears on an explicit click, so focusing it is what the click asked for. -->
			<Input
				bind:value={draftName}
				type="text"
				class="h-7 text-xs"
				placeholder="Name this view…"
				aria-label="View name"
				autofocus
			/>
		</form>
	{/if}

	{#if views.lastError !== null}
		<p class="problem">{views.lastError}</p>
	{/if}

	{#if views.phase === 'unreadable'}
		<!-- Never rendered as "no saved views": that reads as empty and invites the user to recreate
		     work that is still there. -->
		<p class="problem">
			Your saved views could not be read ({views.detail}). They have not been changed.
		</p>
		<Button
			variant="outline"
			size="xs"
			class="mx-0.5 self-start"
			disabled={views.busy}
			title="Discard the saved views that could not be read, so views can be saved again"
			onclick={() => void views.reset()}
		>
			Reset views
		</Button>
	{:else if views.views.length === 0}
		<p class="empty">No saved views yet. Arrange the dock, then press +.</p>
	{:else}
		<ul>
			{#each views.views as v (v.id)}
				{@const isActive = v.id === views.activeId}
				<li>
					<Button
						variant={isActive ? 'secondary' : 'ghost'}
						size="sm"
						class="min-w-0 flex-1 justify-start"
						onclick={() => onselect(v.id)}
					>
						{#if isActive}<Check size={13} />{/if}
						<span class="truncate">{v.name}</span>
						<!-- The whole point of the state model, in one dot. -->
						{#if isActive && views.dirty}<span class="dot" title="Modified since it was saved"
								>●</span
							>{/if}
					</Button>
					{#if isActive}
						<Button
							variant="ghost"
							size="icon-xs"
							title="Save the current arrangement into this view"
							aria-label="Save view"
							disabled={!views.dirty || views.busy}
							onclick={() => void views.save()}
						>
							<Save size={13} />
						</Button>
					{/if}
					<Button
						variant="ghost"
						size="icon-xs"
						title="Delete this view"
						aria-label="Delete view"
						disabled={views.busy}
						onclick={() => void views.remove(v.id)}
					>
						<Trash2 size={13} />
					</Button>
				</li>
			{/each}
		</ul>
	{/if}
</aside>

<style>
	.views {
		display: flex;
		flex: 0 0 auto;
		flex-direction: column;
		gap: 4px;
		width: 200px;
		padding: 8px;
		border-inline-end: 1px solid var(--border);
		background: var(--background);
		overflow-y: auto;
	}
	/* Above the dock on a narrow screen (the workbench page stacks them): full width, and short
	   enough to leave the panels the height. */
	@media (width < 768px) {
		.views {
			width: auto;
			max-height: 9rem;
			border-inline-end: 0;
			border-bottom: 1px solid var(--border);
		}
	}
	header {
		display: flex;
		align-items: center;
		justify-content: space-between;
	}
	.heading {
		font-size: 11px;
		font-weight: 500;
		text-transform: uppercase;
		letter-spacing: 0.04em;
		color: var(--muted-foreground);
	}
	ul {
		display: flex;
		flex-direction: column;
		gap: 1px;
		margin: 0;
		padding: 0;
		list-style: none;
	}
	li {
		display: flex;
		align-items: center;
		gap: 2px;
	}
	.dot {
		flex: 0 0 auto;
		font-size: 9px;
		color: var(--warning, var(--muted-foreground));
	}
	.empty,
	.problem {
		margin: 4px 2px;
		font-size: 11px;
		color: var(--muted-foreground);
	}
	.problem {
		color: var(--destructive);
	}
</style>
