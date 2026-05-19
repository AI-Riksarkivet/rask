/**
 * CSF3 interaction tests for NamespacePermissionsEditor.
 *
 * Tests adding/removing namespaces and toggling permissions.
 */
import type { Meta, StoryObj } from "@storybook/svelte";
import { expect, userEvent, within } from "storybook/test";
import TestHarness from "./namespace-permissions-editor-test-harness.svelte";

const meta = {
  title: "Tests/NamespacePermissionsEditor Interactions",
  // deno-lint-ignore no-explicit-any -- Storybook Meta types incompatible with Svelte 5 Component
  component: TestHarness as any,
  tags: ["!autodocs"],
} satisfies Meta<typeof TestHarness>;

export default meta;
type Story = StoryObj<typeof meta>;

/**
 * Verify initial state renders two namespaces with permissions.
 */
export const RendersInitialState: Story = {
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement);

    await expect(canvas.getByText("production-data")).toBeInTheDocument();
    await expect(canvas.getByText("staging-env")).toBeInTheDocument();
    await expect(canvas.getByText("Namespace Access")).toBeInTheDocument();
  },
};

/**
 * Toggle a permission badge and verify it changes state.
 */
export const TogglePermission: Story = {
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement);

    // Find all WRITE badges — staging-env has it enabled, production-data does not
    const writeBadges = canvas.getAllByText("WRITE");
    // Click WRITE on production-data (first namespace) to enable it
    await userEvent.click(writeBadges[0]);

    // The save button should now be enabled (dirty)
    const saveBtn = canvas.getByRole("button", { name: /save/i });
    await expect(saveBtn).not.toBeDisabled();
  },
};

/**
 * Remove a namespace entry.
 */
export const RemoveNamespace: Story = {
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement);

    // Verify staging-env exists as a namespace card label
    await expect(canvas.getByText("staging-env")).toBeInTheDocument();

    // Find the delete buttons (Trash2 icons) — there should be 2
    const deleteButtons = canvasElement.querySelectorAll(
      'button[class*="hover:text-destructive"]',
    );
    await expect(deleteButtons.length).toBe(2);

    // Click the second delete button (staging-env)
    await userEvent.click(deleteButtons[1] as HTMLElement);

    // After removal, staging-env moves from the card list to the <option> dropdown.
    // Verify the namespace card is removed by checking only 1 delete button remains.
    const remaining = canvasElement.querySelectorAll(
      'button[class*="hover:text-destructive"]',
    );
    await expect(remaining.length).toBe(1);

    // The remaining card should be production-data
    await expect(canvas.getByText("production-data")).toBeInTheDocument();
  },
};
