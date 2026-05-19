<script lang="ts">
	import { Badge } from '$lib/components/ui/badge/index.js';
	import { GitBranch, FolderOpen, Database, Cloud, Mail } from 'lucide-svelte';

	interface Props {
		tag: string;
	}

	let { tag }: Props = $props();

	const SERVICE_TAGS: Record<string, { icon: typeof GitBranch; label: string; class: string }> = {
		lakefs: {
			icon: GitBranch,
			label: 'LakeFS',
			class: 'bg-green-500/15 text-green-700 dark:text-green-400',
		},
		nfs: {
			icon: FolderOpen,
			label: 'NFS',
			class: 'bg-blue-500/15 text-blue-700 dark:text-blue-400',
		},
		cifs: {
			icon: FolderOpen,
			label: 'CIFS',
			class: 'bg-purple-500/15 text-purple-700 dark:text-purple-400',
		},
		hdfs: {
			icon: Database,
			label: 'HDFS',
			class: 'bg-orange-500/15 text-orange-700 dark:text-orange-400',
		},
		s3: {
			icon: Cloud,
			label: 'S3',
			class: 'bg-sky-500/15 text-sky-700 dark:text-sky-400',
		},
		smtp: {
			icon: Mail,
			label: 'SMTP',
			class: 'bg-red-500/15 text-red-700 dark:text-red-400',
		},
	};

	const service = $derived(tag ? SERVICE_TAGS[tag.toLowerCase()] : undefined);
</script>

{#if service}
	<span
		class="inline-flex items-center gap-1 rounded-md px-1.5 py-0.5 text-xs font-medium {service.class}"
	>
		<service.icon class="h-3 w-3" />
		{service.label}
	</span>
{:else}
	<Badge variant="secondary" class="text-xs">{tag}</Badge>
{/if}
