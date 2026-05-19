/**
 * CSF3 interaction tests for FileViewer.
 *
 * Tests rendering, navigation between files, metadata panel toggle, and close.
 * Note: Dialog renders via a portal, so queries use document.body with findBy*.
 * The harness also shows the filename in a data-testid element, so we use
 * findByRole("heading") to target the dialog title specifically.
 *
 * Run `make storybook` and open the "Interactions" panel to see results.
 */
import type { Meta, StoryObj } from "@storybook/svelte";
import { expect, userEvent, within } from "storybook/test";
import FileViewerTestHarness from "./FileViewer-test-harness.svelte";

const meta = {
  title: "Tests/FileViewer Interactions",
  // deno-lint-ignore no-explicit-any -- Storybook Meta types incompatible with Svelte 5 Component
  component: FileViewerTestHarness as any,
  tags: ["!autodocs"],
} satisfies Meta<FileViewerTestHarness>;

export default meta;
type Story = StoryObj<typeof meta>;

function getPage() {
  return within(document.body);
}

/**
 * Verify the viewer opens with the first file's details.
 */
export const RendersFirstFile: Story = {
  play: async () => {
    const page = getPage();

    // Use heading role to target the dialog title (not the harness data-testid div)
    await expect(
      await page.findByRole("heading", { name: "photo-001.jpg" }),
    ).toBeInTheDocument();
    await expect(await page.findByText("1 of 3")).toBeInTheDocument();
    await expect(await page.findByText("240.0 KB")).toBeInTheDocument();
    await expect(await page.findByText("STANDARD")).toBeInTheDocument();
  },
};

/**
 * Navigate forward to the next file.
 */
export const NavigateNext: Story = {
  play: async ({ canvasElement }) => {
    const page = getPage();
    const canvas = within(canvasElement);

    // Click "Next file" button
    const nextBtn = await page.findByRole("button", { name: "Next file" });
    await userEvent.click(nextBtn);

    // Should now show second file
    await expect(
      await page.findByRole("heading", { name: "photo-002.png" }),
    ).toBeInTheDocument();
    await expect(await page.findByText("2 of 3")).toBeInTheDocument();

    // Verify the harness state also updated
    await expect(canvas.getByTestId("current-file")).toHaveTextContent(
      "photo-002.png",
    );
  },
};

/**
 * Navigate to unsupported file type shows download prompt.
 */
export const UnsupportedFileType: Story = {
  play: async () => {
    const page = getPage();

    // Navigate to third file (parquet — unsupported)
    const nextBtn = await page.findByRole("button", { name: "Next file" });
    await userEvent.click(nextBtn);
    await userEvent.click(nextBtn);

    await expect(
      await page.findByRole("heading", { name: "data-export.parquet" }),
    ).toBeInTheDocument();
    await expect(
      await page.findByText(/Preview not available/),
    ).toBeInTheDocument();
    await expect(
      await page.findByText("3 of 3"),
    ).toBeInTheDocument();
  },
};

/**
 * Toggle metadata panel visibility.
 */
export const ToggleMetadata: Story = {
  play: async () => {
    const page = getPage();

    // Metadata should be visible initially (objectKey is set)
    await expect(await page.findByText("Details")).toBeInTheDocument();
    await expect(
      await page.findByText("photos/photo-001.jpg"),
    ).toBeInTheDocument();

    // Click the info toggle button
    const infoBtn = await page.findByRole("button", {
      name: "Toggle metadata",
    });
    await userEvent.click(infoBtn);

    // Details heading should be gone
    await expect(page.queryByText("Details")).not.toBeInTheDocument();

    // Toggle back
    await userEvent.click(infoBtn);
    await expect(await page.findByText("Details")).toBeInTheDocument();
  },
};

/**
 * Close the viewer dialog.
 */
export const CloseViewer: Story = {
  play: async ({ canvasElement }) => {
    const page = getPage();
    const canvas = within(canvasElement);

    // Verify dialog is open
    await expect(
      await page.findByRole("heading", { name: "photo-001.jpg" }),
    ).toBeInTheDocument();

    // Click close button
    const closeBtn = await page.findByRole("button", { name: "Close" });
    await userEvent.click(closeBtn);

    // Reopen button should appear in harness
    await expect(
      await canvas.findByTestId("reopen-btn"),
    ).toBeInTheDocument();
  },
};
