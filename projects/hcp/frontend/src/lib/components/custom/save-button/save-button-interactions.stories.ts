/**
 * CSF3 interaction tests for SaveButton.
 *
 * Tests disabled state, dirty enable, and save callback.
 */
import type { Meta, StoryObj } from "@storybook/svelte";
import { expect, userEvent, within } from "storybook/test";
import TestHarness from "./save-button-test-harness.svelte";

const meta = {
  title: "Tests/SaveButton Interactions",
  // deno-lint-ignore no-explicit-any -- Storybook Meta types incompatible with Svelte 5 Component
  component: TestHarness as any,
  tags: ["!autodocs"],
} satisfies Meta<typeof TestHarness>;

export default meta;
type Story = StoryObj<typeof meta>;

/**
 * Save button should be disabled when not dirty.
 */
export const DisabledWhenClean: Story = {
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement);

    const saveBtn = canvas.getByRole("button", { name: /save/i });
    await expect(saveBtn).toBeDisabled();
  },
};

/**
 * Making the form dirty should enable the save button and show "Unsaved changes".
 */
export const EnablesWhenDirty: Story = {
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement);

    // Initially disabled
    const saveBtn = canvas.getByRole("button", { name: /save/i });
    await expect(saveBtn).toBeDisabled();

    // Make dirty
    await userEvent.click(canvas.getByTestId("make-dirty"));

    // Now enabled with "Unsaved changes" text
    await expect(saveBtn).not.toBeDisabled();
    await expect(canvas.getByText("Unsaved changes")).toBeInTheDocument();
  },
};

/**
 * Clicking save should increment the save counter and reset to clean.
 */
export const SaveResetsToClean: Story = {
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement);

    // Make dirty then save
    await userEvent.click(canvas.getByTestId("make-dirty"));
    const saveBtn = canvas.getByRole("button", { name: /save/i });
    await userEvent.click(saveBtn);

    // Wait for save to complete (500ms + buffer)
    await new Promise((r) => setTimeout(r, 700));

    // Counter should show 1 save
    await expect(canvas.getByTestId("save-count")).toHaveTextContent(
      "Saves: 1",
    );

    // Button should be disabled again (clean state)
    await expect(
      canvas.getByRole("button", { name: /save/i }),
    ).toBeDisabled();
  },
};
