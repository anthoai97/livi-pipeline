/** One tracker per mounted manifest, independent of three.js's global loading manager. */
export function trackModels(keys: string[]) {
  const expected = new Set(keys);
  const settled = new Map<string, boolean>();
  let loadedAt: number | null = expected.size === 0 ? performance.now() : null;
  return {
    settle(key: string, failed: boolean) {
      if (!expected.has(key) || settled.has(key)) return;
      settled.set(key, failed);
      if (settled.size === expected.size) loadedAt = performance.now();
    },
    snapshot() {
      return {
        models: expected.size,
        settled: settled.size,
        failed_models: [...settled.values()].filter(Boolean).length,
        loadedAt,
      };
    },
  };
}
