<script lang="ts">
	import { navigating } from '$app/state';
</script>

{#if navigating.to}
	<div class="navigation-progress" aria-hidden="true">
		<div class="navigation-progress-bar"></div>
	</div>
{/if}

<style>
	.navigation-progress {
		position: fixed;
		top: 0;
		left: 0;
		right: 0;
		height: 2px;
		z-index: 100;
		overflow: hidden;
	}

	.navigation-progress-bar {
		height: 100%;
		background: var(--color-primary);
		animation: progress 2s ease-in-out infinite;
	}

	@keyframes progress {
		0% {
			width: 0%;
			margin-left: 0%;
		}
		50% {
			width: 70%;
			margin-left: 15%;
		}
		100% {
			width: 0%;
			margin-left: 100%;
		}
	}
</style>
