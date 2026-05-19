<script lang="ts">
	import TenantSwitcher from './TenantSwitcher.svelte';

	const now = Math.floor(Date.now() / 1000);
	const future = now + 60 * 60 * 4;
	const past = now - 60 * 60;

	const sessions = [
		{
			tenant: 'dev-ai',
			username: 'admin',
			exp: future,
			expired: false,
			cookieName: 'hcp_token__dev-ai__admin',
			isActive: true,
		},
		{
			tenant: 'prod-ai',
			username: 'admin',
			exp: future,
			expired: false,
			cookieName: 'hcp_token__prod-ai__admin',
			isActive: false,
		},
		{
			tenant: 'staging',
			username: 'operator',
			exp: past,
			expired: true,
			cookieName: 'hcp_token__staging__operator',
			isActive: false,
		},
	];
</script>

<div class="p-4">
	<TenantSwitcher {sessions} currentTenant="dev-ai" />
</div>
