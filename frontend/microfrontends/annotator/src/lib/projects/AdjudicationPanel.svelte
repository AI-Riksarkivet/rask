<script lang="ts">
	// Consensus v1's merge step, as a surface: one card per replica group, the manager PICKS the
	// canonical replica — never blends them. Renders only when replica groups exist. The pick is
	// server-gated (can_manage) and server-validated (accepted members only, refused once
	// provenance freezes); a refusal here is the SERVER's 403/409, surfaced verbatim.
	import { Badge } from '@rask/ui/badge';
	import { Button } from '@rask/ui/button';
	import { Card } from '@rask/ui/card';

	import { adjudicate, clearAdjudication } from './remote/projects.remote';
	import { taskStateVariant } from './presentation.js';
	import type { Adjudication, TaskDetail } from './types.js';

	let {
		projectId,
		tasks,
		adjudications,
		onchanged,
	}: {
		projectId: string;
		tasks: TaskDetail[];
		adjudications: Record<string, Adjudication>;
		/** Fired after a successful pick so the page refetches ONE snapshot. */
		onchanged: () => void;
	} = $props();

	const groups = $derived.by(() => {
		const byGroup = new Map<string, TaskDetail[]>();
		for (const task of tasks) {
			if (!task.replica_of) continue;
			const members = byGroup.get(task.replica_of) ?? [];
			members.push(task);
			byGroup.set(task.replica_of, members);
		}
		return [...byGroup.entries()]
			.map(([group, members]) => ({
				group,
				members: members.toSorted((a, b) => a.task_id.localeCompare(b.task_id)),
			}))
			.toSorted((a, b) => a.group.localeCompare(b.group));
	});

	let busy = $state<string | null>(null); // the group being picked
	let notice = $state('');

	async function pick(group: string, taskId: string): Promise<void> {
		if (busy) return;
		busy = group;
		notice = '';
		const result = await adjudicate({ projectId, groupId: group, taskId });
		busy = null;
		if (result.ok) {
			onchanged();
			return;
		}
		notice = result.detail;
	}

	async function withdraw(group: string): Promise<void> {
		if (busy) return;
		busy = group;
		notice = '';
		const result = await clearAdjudication({ projectId, groupId: group });
		busy = null;
		if (result.ok) {
			onchanged();
			return;
		}
		notice = result.detail;
	}

	function replicaK(taskId: string): string {
		return /-r(\d+)$/.exec(taskId)?.[1] ?? taskId;
	}
</script>

{#if groups.length > 0}
	<Card class="flex flex-col gap-3 p-4" data-testid="adjudication-panel">
		<div>
			<h2 class="text-sm font-semibold">Adjudication</h2>
			<p class="text-muted-foreground text-xs">
				Pick the canonical replica per group — every replica still publishes; the pick travels in
				the run facet with your name on it. Only accepted work can be canonical.
			</p>
		</div>
		{#if notice}
			<p class="text-destructive text-xs">{notice}</p>
		{/if}
		{#each groups as { group, members } (group)}
			{@const current = adjudications[group]}
			<div class="flex flex-col gap-1.5 border-t pt-2 first:border-t-0 first:pt-0">
				<span class="text-muted-foreground font-mono text-xs" title="replica group {group}"
					>{group}</span
				>
				{#each members as member (member.task_id)}
					{@const isCanonical = current?.task_id === member.task_id}
					<div class="flex items-center gap-2 text-xs">
						<span class="font-mono">r{replicaK(member.task_id)}</span>
						<Badge variant={taskStateVariant(member.state)}>{member.state.replace('_', ' ')}</Badge>
						{#if member.submitted_by}
							<span class="text-muted-foreground">{member.submitted_by}</span>
						{/if}
						{#if isCanonical}
							{#if member.state === 'accepted'}
								<Badge title="the manager's canonical pick — picked by {current?.by}"
									>canonical</Badge
								>
							{:else}
								<!-- The pick's target left `accepted` since it was made. The server voids it on
								     the state report and the publish refuses it regardless — this names the
								     window honestly instead of rendering a confident chip. -->
								<Badge
									variant="destructive"
									title="picked by {current?.by}, but this replica is no longer accepted — withdraw or re-pick"
								>
									stale pick
								</Badge>
							{/if}
							<Button
								variant="ghost"
								size="sm"
								class="ml-auto h-6 px-2 text-xs"
								disabled={busy !== null}
								title="withdraw this group's pick"
								onclick={() => void withdraw(group)}
							>
								Withdraw
							</Button>
						{:else if member.state === 'accepted'}
							<Button
								variant="outline"
								size="sm"
								class="ml-auto h-6 px-2 text-xs"
								disabled={busy !== null}
								onclick={() => void pick(group, member.task_id)}
							>
								{current ? 'Re-pick' : 'Pick'}
							</Button>
						{/if}
					</div>
				{/each}
			</div>
		{/each}
	</Card>
{/if}
