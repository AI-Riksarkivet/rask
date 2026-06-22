import type { Preview } from '@storybook/svelte';
import '../src/lib/styles/tokens.css';

const preview: Preview = {
	tags: ['autodocs'],
	parameters: {
		controls: {
			matchers: {
				color: /(background|color)$/i,
				date: /Date$/i,
			},
		},
	},
};

export default preview;
