<script module lang="ts">
	import { defineMeta } from '@storybook/addon-svelte-csf';
	import ServiceTagBadge from './service-tag-badge.svelte';

	const { Story } = defineMeta({
		title: 'UI/ServiceTagBadge',
		component: ServiceTagBadge,
		args: {
			tag: 's3',
		},
		argTypes: {
			tag: {
				control: 'select',
				options: ['s3', 'nfs', 'cifs', 'hdfs', 'smtp', 'lakefs', 'custom-service'],
			},
		},
	});
</script>

<Story name="S3" args={{ tag: 's3' }} />
<Story name="NFS" args={{ tag: 'nfs' }} />
<Story name="CIFS" args={{ tag: 'cifs' }} />
<Story name="HDFS" args={{ tag: 'hdfs' }} />
<Story name="SMTP" args={{ tag: 'smtp' }} />
<Story name="LakeFS" args={{ tag: 'lakefs' }} />
<Story name="Unknown Tag" args={{ tag: 'custom-service' }} />

<Story name="All Variants">
	{#snippet template()}
		<div class="flex flex-wrap items-center gap-3">
			<ServiceTagBadge tag="s3" />
			<ServiceTagBadge tag="nfs" />
			<ServiceTagBadge tag="cifs" />
			<ServiceTagBadge tag="hdfs" />
			<ServiceTagBadge tag="smtp" />
			<ServiceTagBadge tag="lakefs" />
			<ServiceTagBadge tag="custom-service" />
		</div>
	{/snippet}
</Story>
