// SPDX-License-Identifier: Apache-2.0
/**
 * usePromptQueue — FIFO prompt queue for Buddy (CoworkDesktop + Office).
 *
 * The queue is stored in a ref (not state) so enqueue/dequeue operations
 * never trigger re-renders. The consuming component maintains a separate
 * `queuedCount` state for UI updates, incrementing/decrementing it after
 * each enqueue/dequeue/removeAt call.
 *
 * @param {number} maxWait - Maximum queued messages (0 = unlimited).
 */
import { useRef, useCallback } from "react";

export function usePromptQueue(maxWait) {
  // Array of { text, attachments, timestamp }
  const queueRef = useRef([]);

  /**
   * Add a payload to the tail of the queue (FIFO).
   * Returns true if enqueued, false if the queue is full.
   */
  const enqueue = useCallback(
    (payload) => {
      if (maxWait > 0 && queueRef.current.length >= maxWait) {
        return false; // queue full
      }
      queueRef.current = [
        ...queueRef.current,
        { ...payload, timestamp: Date.now() },
      ];
      return true;
    },
    [maxWait]
  );

  /**
   * Remove and return the head of the queue (FIFO).
   * Returns null if the queue is empty.
   */
  const dequeueNext = useCallback(() => {
    if (queueRef.current.length === 0) return null;
    const [next, ...rest] = queueRef.current;
    queueRef.current = rest;
    return next;
  }, []);

  /**
   * Remove a single queued message by its zero-based index.
   * Returns true if the item was found and removed, false otherwise.
   */
  const removeAt = useCallback((index) => {
    if (index < 0 || index >= queueRef.current.length) return false;
    queueRef.current = queueRef.current.filter((_, i) => i !== index);
    return true;
  }, []);

  /**
   * Discard all queued messages.
   */
  const clearQueue = useCallback(() => {
    queueRef.current = [];
  }, []);

  /**
   * Return a snapshot of the current queue (safe to render).
   * Does not mutate the ref.
   */
  const getQueue = useCallback(() => [...queueRef.current], []);

  /**
   * Return the current queue length.
   */
  const queueLength = useCallback(() => queueRef.current.length, []);

  /**
   * Return true if the queue is at capacity (maxWait > 0 and full).
   */
  const isFull = useCallback(
    () => maxWait > 0 && queueRef.current.length >= maxWait,
    [maxWait]
  );

  return { enqueue, dequeueNext, removeAt, clearQueue, getQueue, queueLength, isFull };
}
