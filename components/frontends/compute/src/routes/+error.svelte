<script lang="ts">
	import { page } from '$app/state';
	import { AppError } from '@rask/ui/shell';
</script>

<AppError status={page.status} message={page.error?.message} />
