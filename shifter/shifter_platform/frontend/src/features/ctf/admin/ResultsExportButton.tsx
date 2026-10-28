import { useState } from "react";

import { exportCtfResults } from "@/api/ctfAdmin";
import { describeMutationError } from "@/api/errors";
import { Button } from "@/components/ui/button";

/** Download the event results/statistics export as JSON (CTF-1103). */
export function ResultsExportButton({ eventId }: Readonly<{ eventId: string }>) {
  const [error, setError] = useState<unknown>(null);

  async function download() {
    setError(null);
    try {
      const data = await exportCtfResults(eventId);
      const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" });
      const href = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = href;
      anchor.download = "event-results.json";
      anchor.click();
      URL.revokeObjectURL(href);
    } catch (error_) {
      setError(error_);
    }
  }

  return (
    <div className="flex flex-col items-end gap-1">
      <Button type="button" variant="outline" size="sm" onClick={() => void download()}>
        Export results
      </Button>
      {error ? (
        <p className="text-xs text-destructive">{describeMutationError(error, "Could not export the results.")}</p>
      ) : null}
    </div>
  );
}
