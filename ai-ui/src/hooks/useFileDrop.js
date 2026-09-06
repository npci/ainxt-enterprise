// SPDX-License-Identifier: MIT
// ai-ui/src/hooks/useFileDrop.js
import { useState, useCallback, useEffect, useRef } from 'react';
export function useFileDrop({ onFiles, accept, disabled } = {}) {
  const [isDragging, setIsDragging] = useState(false);
  const dragCounter = useRef(0);
    // Keep latest onFiles/accept/disabled in a ref so native listeners always
    // see the current values without needing to be re-registered.
    const onFilesRef  = useRef(onFiles);
    const acceptRef   = useRef(accept);
    const disabledRef = useRef(disabled);
    useEffect(() => { onFilesRef.current  = onFiles;  }, [onFiles]);
    useEffect(() => { acceptRef.current   = accept;   }, [accept]);
    useEffect(() => { disabledRef.current = disabled; }, [disabled]);

    // BUG FIX (drag-and-drop not working in KnowledgeBase):
    // Previously this hook used a plain `useRef(null)` plus a one-shot
    // `useEffect(..., [])` to register listeners. That only worked when the
    // drop-zone element existed at mount time. In KnowledgeBase the drop
    // zone is mounted lazily — it lives behind the "Upload" tab (default tab
    // is "Chat") and is also unmounted whenever an upload is in progress or
    // a compliance block is shown. By the time the element appears the
    // effect has already fired with `dropRef.current === null` and never
    // re-runs, so no listeners get attached.
    //
    // Fix: use a ref-callback. React invokes it with the node when it
    // mounts and with `null` when it unmounts, so we can attach/detach
    // listeners every time the drop-zone enters/leaves the DOM — no matter
    // how late it appears.
    const cleanupRef = useRef(null);

    const dropRef = useCallback((node) => {
      // Tear down any previous listeners (unmount or node swap).
      if (cleanupRef.current) {
        cleanupRef.current();
        cleanupRef.current = null;
      }
      if (!node) return;

      function onDragEnter(e) {
        if (disabledRef.current) return;
        e.preventDefault();
        e.stopPropagation();
        dragCounter.current += 1;
        setIsDragging(true);
      }

      function onDragLeave(e) {
        if (disabledRef.current) return;
        e.preventDefault();
        e.stopPropagation();
        dragCounter.current -= 1;
        if (dragCounter.current <= 0) {
          dragCounter.current = 0;
          setIsDragging(false);
        }
      }

      // CRITICAL: must be { passive: false } so preventDefault() actually works
      // and suppresses the browser's own "security" popup for drag-and-drop.
      function onDragOver(e) {
        e.preventDefault();           // ← suppresses browser security popup
        e.stopPropagation();
        if (disabledRef.current) return;
        e.dataTransfer.dropEffect = 'copy';
      }

      function onDrop(e) {
        e.preventDefault();
        e.stopPropagation();
        dragCounter.current = 0;
        setIsDragging(false);
        if (disabledRef.current) return;

        const droppedFiles = Array.from(e.dataTransfer.files || []);
        if (droppedFiles.length === 0) return;

        const currentAccept = acceptRef.current;
        const validFiles = currentAccept
          ? droppedFiles.filter(file =>
              currentAccept.some(pattern => {
                if (pattern.endsWith('/*')) {
                  const baseType = pattern.slice(0, -2);
                  return file.type.startsWith(baseType + '/');
                }
                return file.type === pattern;
              })
            )
          : droppedFiles;

        const invalidFiles = droppedFiles.filter(f => !validFiles.includes(f));

        if (invalidFiles.length > 0 && validFiles.length === 0) {
          onFilesRef.current?.([], invalidFiles);
          return;
        }

        if (validFiles.length > 0) {
          onFilesRef.current?.(validFiles, invalidFiles);
        }
      }

      // { passive: false } is required for preventDefault() to work on dragover
      node.addEventListener('dragenter', onDragEnter);
      node.addEventListener('dragleave', onDragLeave);
      node.addEventListener('dragover',  onDragOver,  { passive: false });
      node.addEventListener('drop',      onDrop);

      cleanupRef.current = () => {
        node.removeEventListener('dragenter', onDragEnter);
        node.removeEventListener('dragleave', onDragLeave);
        node.removeEventListener('dragover',  onDragOver);
        node.removeEventListener('drop',      onDrop);
        // Reset transient state so a remounted drop zone doesn't start out
        // stuck in the "dragging" visual.
        dragCounter.current = 0;
        setIsDragging(false);
      };
    }, []);

    // Detach listeners if the hook itself unmounts while the node is still
    // attached (defensive — React calls the ref-callback with null first,
    // but this guards against edge cases like Strict-Mode double-invocation).
    useEffect(() => {
      return () => {
        if (cleanupRef.current) {
          cleanupRef.current();
          cleanupRef.current = null;
        }
      };
    }, []);


  return {
    isDragging,
       dropRef,   // attach this ref to the drop-zone element
  };
}
