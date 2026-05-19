<script module lang="ts">
	import { defineMeta } from '@storybook/addon-svelte-csf';
	import TenantSwitcher from './TenantSwitcher.svelte';

	const now = Math.floor(Date.now() / 1000);
	const future = now + 60 * 60 * 4;
	const past = now - 60 * 60;

	const { Story } = defineMeta({
		title: 'Layout/TenantSwitcher',
		component: TenantSwitcher,
	});
</script>

<Story
	name="Single Session"
	args={{
		currentTenant: 'dev-ai',
		sessions: [
			{
				tenant: 'dev-ai',
				username: 'admin',
				exp: future,
				expired: false,
				cookieName: 'hcp_token__dev-ai__admin',
				isActive: true,
			},
		],
	}}
/>

<Story
	name="Multiple Sessions"
	args={{
		currentTenant: 'dev-ai',
		sessions: [
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
				exp: future,
				expired: false,
				cookieName: 'hcp_token__staging__operator',
				isActive: false,
			},
		],
	}}
/>

<Story
	name="With Expired Session"
	args={{
		currentTenant: 'dev-ai',
		sessions: [
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
				exp: past,
				expired: true,
				cookieName: 'hcp_token__prod-ai__admin',
				isActive: false,
			},
		],
	}}
/>

<Story
	name="Same Tenant Different Users"
	args={{
		currentTenant: 'dev-ai',
		sessions: [
			{
				tenant: 'dev-ai',
				username: 'admin',
				exp: future,
				expired: false,
				cookieName: 'hcp_token__dev-ai__admin',
				isActive: true,
			},
			{
				tenant: 'dev-ai',
				username: 'ser_accai_nsm',
				exp: future,
				expired: false,
				cookieName: 'hcp_token__dev-ai__ser_accai_nsm',
				isActive: false,
			},
		],
	}}
/>

<Story
	name="System Admin"
	args={{
		currentTenant: undefined,
		sessions: [
			{
				tenant: undefined,
				username: 'sysadmin',
				exp: future,
				expired: false,
				cookieName: 'hcp_token____sysadmin__sysadmin',
				isActive: true,
			},
			{
				tenant: 'dev-ai',
				username: 'admin',
				exp: future,
				expired: false,
				cookieName: 'hcp_token__dev-ai__admin',
				isActive: false,
			},
		],
	}}
/>
