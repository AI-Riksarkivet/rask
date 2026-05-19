---
hide:
  - toc
  - navigation
---

# UI Components

The HCP App uses [shadcn-svelte](https://next.shadcn-svelte.com/) with custom components built on top. Browse the full interactive component library below.

<iframe
  src="https://ai-riksarkivet.github.io/ra-hcp/storybook/"
  width="100%"
  height="1000"
  style="border: 1px solid #e0e0e0; border-radius: 8px;"
></iframe>

!!! tip "Open in new tab"
    For the best experience, [open Storybook directly](https://ai-riksarkivet.github.io/ra-hcp/storybook/) in a new tab. Use the sidebar to navigate components and the Controls panel to interact with props.

---

## Component Overview

| Component | Description |
|---|---|
| **DataTable** | Sortable, searchable, paginated table with row selection and bulk actions |
| **FileViewer** | Modal for previewing files (images, video, audio, PDF, text) with metadata |
| **FormDialog** | Reusable modal for create/edit forms with validation and error handling |
| **DeleteConfirmDialog** | Destructive action confirmation with type-to-confirm |
| **BulkDeleteDialog** | Multi-item deletion confirmation |
| **NamespacePermissionsEditor** | Manage per-namespace data access permissions (BROWSE, READ, WRITE, etc.) |
| **StorageProgressBar** | Quota usage bar with color-coded thresholds |
| **StatCard** | Dashboard metric card with label, value, and optional content |
| **PageHeader** | Page title with description and action buttons |
| **BackButton** | Navigation back link with tooltip |
| **TagInput** | Tag/chip editor with add/remove for tenant and namespace tags |
| **IpListEditor** | IP allow/deny list editor with badge display |
| **CorsEditor** | CORS XML configuration editor with save/delete |
| **CopyableInput** | Read-only input with copy-to-clipboard, optional secret masking |
| **ServiceTagBadge** | Color-coded badges for protocol types (S3, NFS, CIFS, SMTP) |
| **SaveButton** | Form save button with dirty/saving state indicators |
| **ErrorBanner** | Dismissible error banner for API and validation errors |
| **StepProgress** | Multi-step progress indicator for wizard flows |
| **NoTenantPlaceholder** | Dashed-border placeholder shown when no tenant is selected |

!!! info "Storybook deploys automatically"
    Stories are co-located with components (`*.stories.svelte`) and deployed to GitHub Pages on every push to `main`. The embed above always reflects the latest code -- no manual sync needed.
