export function toggleRunSelection(selected: string[], runId: string): string[] {
  return selected.includes(runId)
    ? selected.filter(item => item !== runId)
    : [...selected, runId];
}

export function togglePageSelection(selected: string[], visibleIds: string[]): string[] {
  return visibleIds.length > 0 && visibleIds.every(id => selected.includes(id))
    ? []
    : [...visibleIds];
}

export function batchDeletionPayload(selectedRunIds: string[]): { run_ids: string[] } {
  return { run_ids: selectedRunIds };
}
