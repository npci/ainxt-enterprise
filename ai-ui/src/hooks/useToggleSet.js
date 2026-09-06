// SPDX-License-Identifier: MIT
// ============================================================
// useToggleSet — a tiny Set-state hook for expand/collapse rows,
// multi-select chips, etc. Returns [set, toggle, has, reset].
// ============================================================
import { useState, useCallback } from "react";

export default function useToggleSet(initial = []) {
  const [set, setSet] = useState(() => new Set(initial));

  const toggle = useCallback((key) => {
    setSet(prev => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  }, []);

  const has = useCallback((key) => set.has(key), [set]);

  const reset = useCallback((keys = []) => setSet(new Set(keys)), []);

  return [set, toggle, has, reset];
}
