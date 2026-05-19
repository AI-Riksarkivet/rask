/**
 * CSF3 interaction tests for TagInput.
 *
 * Tests adding tags (type + Enter), removing tags (click X), and duplicate prevention.
 *
 * Run `make storybook` and open the "Interactions" panel to see results.
 */
import type { Meta, StoryObj } from "@storybook/svelte";
import { expect, userEvent, within } from "storybook/test";
import TagInputTestHarness from "./tag-input-test-harness.svelte";

const meta = {
  title: "Tests/TagInput Interactions",
  // deno-lint-ignore no-explicit-any -- Storybook Meta types incompatible with Svelte 5 Component
  component: TagInputTestHarness as any,
  tags: ["!autodocs"],
} satisfies Meta<TagInputTestHarness>;

export default meta;
type Story = StoryObj<typeof meta>;

/**
 * Verify initial tags render correctly.
 */
export const RendersInitialTags: Story = {
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement);

    await expect(canvas.getByText("production")).toBeInTheDocument();
    await expect(canvas.getByText("critical")).toBeInTheDocument();
    await expect(canvas.getByText("eu-west")).toBeInTheDocument();
    await expect(canvas.getByTestId("tag-count")).toHaveTextContent("3 tag(s)");
  },
};

/**
 * Type a new tag and press Enter to add it.
 */
export const AddTagViaEnter: Story = {
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement);
    const input = canvas.getByPlaceholderText("Add tag...");

    await userEvent.type(input, "new-tag{Enter}");

    await expect(canvas.getByText("new-tag")).toBeInTheDocument();
    await expect(canvas.getByTestId("tag-count")).toHaveTextContent("4 tag(s)");
  },
};

/**
 * Add a tag via the Add button.
 */
export const AddTagViaButton: Story = {
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement);
    const input = canvas.getByPlaceholderText("Add tag...");
    const addButton = canvas.getByRole("button", { name: "Add" });

    await userEvent.type(input, "button-tag");
    await userEvent.click(addButton);

    await expect(canvas.getByText("button-tag")).toBeInTheDocument();
    await expect(canvas.getByTestId("tag-count")).toHaveTextContent("4 tag(s)");
  },
};

/**
 * Duplicate tags should not be added.
 */
export const PreventsDuplicates: Story = {
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement);
    const input = canvas.getByPlaceholderText("Add tag...");

    // Try to add an existing tag
    await userEvent.type(input, "production{Enter}");

    // Count should remain 3
    await expect(canvas.getByTestId("tag-count")).toHaveTextContent("3 tag(s)");
  },
};

/**
 * Remove a tag by clicking the X button.
 */
export const RemoveTag: Story = {
  play: async ({ canvasElement }) => {
    const canvas = within(canvasElement);

    // The X buttons are inside each badge <span>
    const criticalBadge = canvas
      .getByText("critical")
      .closest("[data-slot='badge']")!;
    const removeButton = within(criticalBadge as HTMLElement).getByRole(
      "button",
    );
    await userEvent.click(removeButton);

    await expect(canvas.queryByText("critical")).not.toBeInTheDocument();
    await expect(canvas.getByTestId("tag-count")).toHaveTextContent("2 tag(s)");
  },
};
