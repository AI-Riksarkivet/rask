<script module lang="ts">
	import { defineMeta } from '@storybook/addon-svelte-csf';
	import { fn } from 'storybook/test';
	import CorsEditor from './cors-editor.svelte';

	const { Story } = defineMeta({
		title: 'UI/CorsEditor',
		component: CorsEditor,
		args: {
			corsXml: '',
			loading: false,
			title: 'CORS Configuration',
			description: '',
			onsave: fn(),
			ondelete: fn(),
		},
	});
</script>

<Story name="Empty" args={{ corsXml: '' }} />

<Story
	name="With Configuration"
	args={{
		corsXml: `<CORSConfiguration>
  <CORSRule>
    <AllowedOrigin>https://example.com</AllowedOrigin>
    <AllowedMethod>GET</AllowedMethod>
    <AllowedMethod>PUT</AllowedMethod>
    <AllowedHeader>*</AllowedHeader>
    <MaxAgeSeconds>3600</MaxAgeSeconds>
  </CORSRule>
</CORSConfiguration>`,
	}}
/>

<Story name="Loading" args={{ corsXml: '', loading: true, ondelete: undefined }} />

<Story
	name="Without Delete"
	args={{
		corsXml: `<CORSConfiguration>
  <CORSRule>
    <AllowedOrigin>*</AllowedOrigin>
    <AllowedMethod>GET</AllowedMethod>
  </CORSRule>
</CORSConfiguration>`,
		ondelete: undefined,
	}}
/>

<Story
	name="Custom Title"
	args={{
		corsXml: '',
		title: 'Namespace CORS',
		description: 'Configure cross-origin resource sharing rules for this namespace.',
		ondelete: undefined,
	}}
/>
