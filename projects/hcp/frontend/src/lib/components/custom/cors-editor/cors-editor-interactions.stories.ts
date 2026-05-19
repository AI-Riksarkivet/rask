/**
 * CSF3 interaction tests for CorsEditor.
 *
 * Tests editing XML, saving, and deleting CORS configuration.
 *
 * Run `make storybook` and open the "Interactions" panel to see results.
 */
import type { Meta, StoryObj } from "@storybook/svelte";
import { expect, userEvent, within } from "storybook/test";
import CorsEditorTestHarness from "./cors-editor-test-harness.svelte";

const meta = {
  title: "Tests/CorsEditor Interactions",
  // deno-lint-ignore no-explicit-any -- Storybook Meta types incompatible with Svelte 5 Component
  component: CorsEditorTestHarness as any,
  tags: ["!autodocs"],
} satisfies Meta<CorsEditorTestHarness>;

export default meta;
type Story = StoryObj<typeof meta>;

/**
 * Verify the editor renders with initial XML content.
 */
export const RendersWithContent: Story = {
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement);

    await expect(canvas.getByText("CORS Configuration")).toBeInTheDocument();
    await expect(canvas.getByText("Delete CORS")).toBeInTheDocument();

    // The textarea should contain the initial XML
    const textarea = canvas.getByRole("textbox") as HTMLTextAreaElement;
    await expect(textarea.value).toContain("<CORSConfiguration>");
  },
};

/**
 * Edit XML and verify the Save button becomes enabled (dirty).
 */
export const EditMakesDirty: Story = {
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement);
    const textarea = canvas.getByRole("textbox");

    // Save button should be disabled initially (not dirty)
    const saveButton = canvas.getByRole("button", { name: /save/i });
    await expect(saveButton).toBeDisabled();

    // Type additional content
    await userEvent.type(textarea, "\n<!-- modified -->");

    // Save button should now be enabled
    await expect(saveButton).not.toBeDisabled();
  },
};

/**
 * Edit and save the CORS configuration.
 */
export const SaveConfiguration: Story = {
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement);
    const textarea = canvas.getByRole("textbox");

    // Make a change to enable save
    await userEvent.type(textarea, "\n<!-- saved -->");

    // Click save
    const saveButton = canvas.getByRole("button", { name: /save/i });
    await userEvent.click(saveButton);

    // Verify save was called (async — wait for the handler to complete)
    await expect(
      await canvas.findByTestId("save-result"),
    ).toHaveTextContent("CORS saved");
  },
};
