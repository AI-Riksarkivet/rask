import { toast } from "svelte-sonner";
import { getErrorMessage } from "$lib/utils/get-error-message.js";

interface UseSaveOptions {
  successMsg: string;
  errorMsg: string;
}

export function useSave(opts: UseSaveOptions) {
  let saving = $state(false);
  let syncVersion = $state(0);

  async function run(fn: () => Promise<unknown>) {
    saving = true;
    try {
      await fn();
      syncVersion++;
      toast.success(opts.successMsg);
    } catch (err) {
      toast.error(getErrorMessage(err, opts.errorMsg));
    } finally {
      saving = false;
    }
  }

  return {
    get saving() {
      return saving;
    },
    get syncVersion() {
      return syncVersion;
    },
    run,
  };
}
