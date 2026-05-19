import type { Preview } from "@storybook/sveltekit";
import { withThemeByClassName } from "@storybook/addon-themes";
import TooltipProviderDecorator from "./decorators/tooltip-provider-decorator.svelte";
import "../src/app.css";

const preview: Preview = {
  // Autodocs for all stories globally — no need to set per-file
  tags: ["autodocs"],
  parameters: {
    controls: {
      matchers: {
        color: /(background|color)$/i,
        date: /Date$/i,
      },
    },
    docs: {
      toc: true,
    },
    a11y: {
      // Fail on accessibility violations by default — use 'todo' per-story to defer fixes
      test: "error",
      config: {
        rules: [
          // Storybook renders stories outside landmark regions — false positive
          { id: "region", enabled: false },
        ],
      },
    },
  },
  decorators: [
    // Provide Tooltip.Provider context for all stories (Bits UI requires this)
    () => ({
      Component: TooltipProviderDecorator,
    }),
    withThemeByClassName({
      themes: {
        light: "",
        dark: "dark",
      },
      defaultTheme: "light",
    }),
  ],
};

export default preview;
