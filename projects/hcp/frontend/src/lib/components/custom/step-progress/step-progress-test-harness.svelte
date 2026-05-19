<script lang="ts">
	type Step = {
		label: string;
		group?: string;
		status: 'pending' | 'running' | 'done' | 'failed';
		error?: string;
	};

	import StepProgress from './step-progress.svelte';
	import { Button } from '$lib/components/ui/button/index.js';

	let steps = $state<Step[]>([
		{ label: 'Validate configuration', status: 'pending' },
		{ label: 'Create namespace', status: 'pending' },
		{ label: 'Apply permissions', status: 'pending' },
	]);

	let running = $state(false);

	function runSteps() {
		running = true;
		steps[0].status = 'running';

		setTimeout(() => {
			steps[0].status = 'done';
			steps[1].status = 'running';
		}, 400);

		setTimeout(() => {
			steps[1].status = 'done';
			steps[2].status = 'running';
		}, 800);

		setTimeout(() => {
			steps[2].status = 'done';
			running = false;
		}, 1200);
	}

	function runWithError() {
		running = true;
		steps = [
			{ label: 'Validate configuration', status: 'pending' },
			{ label: 'Create namespace', status: 'pending' },
			{ label: 'Apply permissions', status: 'pending' },
		];
		steps[0].status = 'running';

		setTimeout(() => {
			steps[0].status = 'done';
			steps[1].status = 'running';
		}, 400);

		setTimeout(() => {
			steps[1].status = 'failed';
			steps[1].error = 'Quota exceeded';
			running = false;
		}, 800);
	}

	function reset() {
		running = false;
		steps = [
			{ label: 'Validate configuration', status: 'pending' },
			{ label: 'Create namespace', status: 'pending' },
			{ label: 'Apply permissions', status: 'pending' },
		];
	}
</script>

<div class="max-w-md space-y-3 p-4">
	<StepProgress {steps} />
	<div class="flex gap-2">
		<Button size="sm" onclick={runSteps} disabled={running} data-testid="run-btn">Run</Button>
		<Button
			size="sm"
			variant="destructive"
			onclick={runWithError}
			disabled={running}
			data-testid="error-btn">Run with Error</Button
		>
		<Button size="sm" variant="outline" onclick={reset} data-testid="reset-btn">Reset</Button>
	</div>
</div>
