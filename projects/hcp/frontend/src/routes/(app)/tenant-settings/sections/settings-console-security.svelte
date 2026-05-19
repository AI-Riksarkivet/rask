<script lang="ts">
	import * as Card from '$lib/components/ui/card/index.js';
	import { Input } from '$lib/components/ui/input/index.js';
	import { Label } from '$lib/components/ui/label/index.js';
	import { Switch } from '$lib/components/ui/switch/index.js';
	import { Textarea } from '$lib/components/ui/textarea/index.js';
	import CardSkeleton from '$lib/components/ui/skeleton/card-skeleton.svelte';
	import SaveButton from '$lib/components/custom/save-button/save-button.svelte';
	import IpListEditor from '$lib/components/custom/ip-list-editor/ip-list-editor.svelte';
	import { useSave } from '$lib/utils/use-save.svelte.js';
	import { arraysEqual } from '$lib/utils/format.js';
	import {
		get_console_security,
		update_console_security,
		type ConsoleSecurity,
	} from '$lib/remote/tenant-info.remote.js';

	let {
		tenant,
	}: {
		tenant: string;
	} = $props();

	let securityData = $derived(get_console_security({ tenant }));
	let security = $derived((securityData?.current ?? {}) as ConsoleSecurity);

	const saver = useSave({
		successMsg: 'Console security updated',
		errorMsg: 'Failed to update console security',
	});

	let localMinPasswordLength = $state(0);
	let localLowerCase = $state(0);
	let localUpperCase = $state(0);
	let localNumeric = $state(0);
	let localSpecial = $state(0);
	let localPasswordReuseDepth = $state(0);
	let localBlockCommonPassword = $state(false);
	let localPasswordContainsUsername = $state(false);
	let localForcePasswordChangeDays = $state(0);
	let localDisableAfterAttempts = $state(0);
	let localCoolDownEnabled = $state(false);
	let localCoolDownDuration = $state(0);
	let localAutoUnlock = $state(false);
	let localAutoUnlockDuration = $state(0);
	let localDisableAfterInactiveDays = $state(0);
	let localLogoutOnInactive = $state(0);
	let localLoginMessage = $state('');
	let localAllowAddresses = $state<string[]>([]);
	let localDenyAddresses = $state<string[]>([]);
	let localAllowIfInBoth = $state(false);

	$effect(() => {
		const s = security;
		void saver.syncVersion;
		localMinPasswordLength = s.minimumPasswordLength ?? 0;
		localLowerCase = s.lowerCaseLetterCount ?? 0;
		localUpperCase = s.upperCaseLetterCount ?? 0;
		localNumeric = s.numericCharacterCount ?? 0;
		localSpecial = s.specialCharacterCount ?? 0;
		localPasswordReuseDepth = s.passwordReuseDepth ?? 0;
		localBlockCommonPassword = s.blockCommonPassword ?? false;
		localPasswordContainsUsername = s.passwordContainsUsername ?? false;
		localForcePasswordChangeDays = s.forcePasswordChangeDays ?? 0;
		localDisableAfterAttempts = s.disableAfterAttempts ?? 0;
		localCoolDownEnabled = s.coolDownPeriodSettings ?? false;
		localCoolDownDuration = s.coolDownPeriodDuration ?? 0;
		localAutoUnlock = s.automaticUserAccountUnlockSetting ?? false;
		localAutoUnlockDuration = s.automaticUserAccoutUnlockDuration ?? 0;
		localDisableAfterInactiveDays = s.disableAfterInactiveDays ?? 0;
		localLogoutOnInactive = s.logoutOnInactive ?? 0;
		localLoginMessage = s.loginMessage ?? '';
		localAllowAddresses = [...(s.ipSettings?.allowAddresses ?? [])];
		localDenyAddresses = [...(s.ipSettings?.denyAddresses ?? [])];
		localAllowIfInBoth = s.ipSettings?.allowIfInBothLists ?? false;
	});

	let dirty = $derived(
		localMinPasswordLength !== (security.minimumPasswordLength ?? 0) ||
			localLowerCase !== (security.lowerCaseLetterCount ?? 0) ||
			localUpperCase !== (security.upperCaseLetterCount ?? 0) ||
			localNumeric !== (security.numericCharacterCount ?? 0) ||
			localSpecial !== (security.specialCharacterCount ?? 0) ||
			localPasswordReuseDepth !== (security.passwordReuseDepth ?? 0) ||
			localBlockCommonPassword !== (security.blockCommonPassword ?? false) ||
			localPasswordContainsUsername !== (security.passwordContainsUsername ?? false) ||
			localForcePasswordChangeDays !== (security.forcePasswordChangeDays ?? 0) ||
			localDisableAfterAttempts !== (security.disableAfterAttempts ?? 0) ||
			localCoolDownEnabled !== (security.coolDownPeriodSettings ?? false) ||
			localCoolDownDuration !== (security.coolDownPeriodDuration ?? 0) ||
			localAutoUnlock !== (security.automaticUserAccountUnlockSetting ?? false) ||
			localAutoUnlockDuration !== (security.automaticUserAccoutUnlockDuration ?? 0) ||
			localDisableAfterInactiveDays !== (security.disableAfterInactiveDays ?? 0) ||
			localLogoutOnInactive !== (security.logoutOnInactive ?? 0) ||
			localLoginMessage !== (security.loginMessage ?? '') ||
			!arraysEqual(localAllowAddresses, security.ipSettings?.allowAddresses ?? []) ||
			!arraysEqual(localDenyAddresses, security.ipSettings?.denyAddresses ?? []) ||
			localAllowIfInBoth !== (security.ipSettings?.allowIfInBothLists ?? false)
	);
</script>

{#await securityData}
	<div class="space-y-6">
		<CardSkeleton />
		<CardSkeleton />
	</div>
{:then}
	<div class="space-y-6">
		<!-- Password Policy -->
		<Card.Root>
			<Card.Header>
				<Card.Title>Password Policy</Card.Title>
				<Card.Description>
					Controls password strength requirements for tenant user accounts.
				</Card.Description>
			</Card.Header>
			<Card.Content class="space-y-4">
				<div class="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
					<div class="space-y-1.5">
						<Label for="min-pwd-len">Minimum Length</Label>
						<Input id="min-pwd-len" type="number" min={0} bind:value={localMinPasswordLength} />
					</div>
					<div class="space-y-1.5">
						<Label for="lower-case">Lowercase Letters</Label>
						<Input id="lower-case" type="number" min={0} bind:value={localLowerCase} />
					</div>
					<div class="space-y-1.5">
						<Label for="upper-case">Uppercase Letters</Label>
						<Input id="upper-case" type="number" min={0} bind:value={localUpperCase} />
					</div>
					<div class="space-y-1.5">
						<Label for="numeric-chars">Numeric Characters</Label>
						<Input id="numeric-chars" type="number" min={0} bind:value={localNumeric} />
					</div>
					<div class="space-y-1.5">
						<Label for="special-chars">Special Characters</Label>
						<Input id="special-chars" type="number" min={0} bind:value={localSpecial} />
					</div>
					<div class="space-y-1.5">
						<Label for="reuse-depth">Reuse Depth</Label>
						<Input id="reuse-depth" type="number" min={0} bind:value={localPasswordReuseDepth} />
						<p class="text-xs text-muted-foreground">
							Number of previous passwords that cannot be reused.
						</p>
					</div>
				</div>
				<div class="space-y-1.5">
					<Label for="force-change-days">Force Password Change (days)</Label>
					<Input
						id="force-change-days"
						type="number"
						min={0}
						class="max-w-48"
						bind:value={localForcePasswordChangeDays}
					/>
					<p class="text-xs text-muted-foreground">0 = never expires.</p>
				</div>
				<div class="flex flex-wrap gap-x-6 gap-y-3">
					<div class="flex items-center gap-2">
						<Switch id="block-common" bind:checked={localBlockCommonPassword} />
						<Label for="block-common" class="text-sm">Block Common Passwords</Label>
					</div>
					<div class="flex items-center gap-2">
						<Switch id="pwd-username" bind:checked={localPasswordContainsUsername} />
						<Label for="pwd-username" class="text-sm">Allow Username in Password</Label>
					</div>
				</div>
			</Card.Content>
		</Card.Root>

		<!-- Account Lockout -->
		<Card.Root>
			<Card.Header>
				<Card.Title>Account Lockout</Card.Title>
				<Card.Description>
					Controls account lockout behavior after failed login attempts.
				</Card.Description>
			</Card.Header>
			<Card.Content class="space-y-4">
				<div class="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
					<div class="space-y-1.5">
						<Label for="disable-attempts">Disable After Attempts</Label>
						<Input
							id="disable-attempts"
							type="number"
							min={0}
							bind:value={localDisableAfterAttempts}
						/>
						<p class="text-xs text-muted-foreground">0 = no lockout.</p>
					</div>
					<div class="space-y-1.5">
						<Label for="cooldown-duration">Cooldown Duration (min)</Label>
						<Input
							id="cooldown-duration"
							type="number"
							min={0}
							bind:value={localCoolDownDuration}
							disabled={!localCoolDownEnabled}
						/>
					</div>
					<div class="space-y-1.5">
						<Label for="auto-unlock-dur">Auto-Unlock Duration (min)</Label>
						<Input
							id="auto-unlock-dur"
							type="number"
							min={0}
							bind:value={localAutoUnlockDuration}
							disabled={!localAutoUnlock}
						/>
					</div>
				</div>
				<div class="space-y-1.5">
					<Label for="disable-inactive">Disable After Inactive (days)</Label>
					<Input
						id="disable-inactive"
						type="number"
						min={0}
						class="max-w-48"
						bind:value={localDisableAfterInactiveDays}
					/>
					<p class="text-xs text-muted-foreground">0 = never disabled for inactivity.</p>
				</div>
				<div class="flex flex-wrap gap-x-6 gap-y-3">
					<div class="flex items-center gap-2">
						<Switch id="cooldown-enabled" bind:checked={localCoolDownEnabled} />
						<Label for="cooldown-enabled" class="text-sm">Cooldown Period</Label>
					</div>
					<div class="flex items-center gap-2">
						<Switch id="auto-unlock" bind:checked={localAutoUnlock} />
						<Label for="auto-unlock" class="text-sm">Auto-Unlock</Label>
					</div>
				</div>
			</Card.Content>
		</Card.Root>

		<!-- Session & Login Message -->
		<Card.Root>
			<Card.Header>
				<Card.Title>Session & Login</Card.Title>
				<Card.Description>Session timeout and login page customization.</Card.Description>
			</Card.Header>
			<Card.Content class="space-y-4">
				<div class="space-y-1.5">
					<Label for="logout-inactive">Session Timeout (min)</Label>
					<Input
						id="logout-inactive"
						type="number"
						min={0}
						class="max-w-48"
						bind:value={localLogoutOnInactive}
					/>
					<p class="text-xs text-muted-foreground">
						Minutes of inactivity before automatic logout. 0 = no timeout.
					</p>
				</div>
				<div class="space-y-1.5">
					<Label for="login-message">Login Message</Label>
					<Textarea
						id="login-message"
						class="min-h-[80px]"
						placeholder="Message displayed on the login page..."
						bind:value={localLoginMessage}
					/>
				</div>
			</Card.Content>
		</Card.Root>

		<!-- IP Restrictions -->
		<Card.Root>
			<Card.Header>
				<Card.Title>IP Restrictions</Card.Title>
				<Card.Description>
					Controls which IP addresses can access the management console for this tenant.
				</Card.Description>
			</Card.Header>
			<Card.Content class="space-y-6">
				<IpListEditor
					bind:addresses={localAllowAddresses}
					label="Allow List"
					emptyText="No allow list configured. All addresses are allowed by default."
				/>
				<IpListEditor
					bind:addresses={localDenyAddresses}
					label="Deny List"
					placeholder="IP address or CIDR (e.g. 192.168.1.0/24)"
					variant="destructive"
					emptyText="No deny list configured."
				/>
				<div class="flex items-center gap-2">
					<Switch id="console-allow-if-both" bind:checked={localAllowIfInBoth} />
					<Label for="console-allow-if-both" class="text-sm">Allow if in both lists</Label>
				</div>
				<p class="text-xs text-muted-foreground">
					When enabled, addresses that appear in both allow and deny lists will be allowed.
				</p>
			</Card.Content>
			<Card.Footer>
				<SaveButton
					{dirty}
					saving={saver.saving}
					onclick={() =>
						saver.run(async () => {
							if (!securityData) return;
							await update_console_security({
								tenant,
								body: {
									minimumPasswordLength: localMinPasswordLength,
									lowerCaseLetterCount: localLowerCase,
									upperCaseLetterCount: localUpperCase,
									numericCharacterCount: localNumeric,
									specialCharacterCount: localSpecial,
									passwordReuseDepth: localPasswordReuseDepth,
									blockCommonPassword: localBlockCommonPassword,
									passwordContainsUsername: localPasswordContainsUsername,
									forcePasswordChangeDays: localForcePasswordChangeDays,
									disableAfterAttempts: localDisableAfterAttempts,
									coolDownPeriodSettings: localCoolDownEnabled,
									coolDownPeriodDuration: localCoolDownDuration,
									automaticUserAccountUnlockSetting: localAutoUnlock,
									automaticUserAccoutUnlockDuration: localAutoUnlockDuration,
									disableAfterInactiveDays: localDisableAfterInactiveDays,
									logoutOnInactive: localLogoutOnInactive,
									loginMessage: localLoginMessage || undefined,
									ipSettings: {
										allowAddresses: localAllowAddresses.filter(Boolean),
										denyAddresses: localDenyAddresses.filter(Boolean),
										allowIfInBothLists: localAllowIfInBoth,
									},
								},
							}).updates(securityData);
						})}
				/>
			</Card.Footer>
		</Card.Root>
	</div>
{/await}
