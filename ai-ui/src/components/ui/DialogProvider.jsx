// SPDX-License-Identifier: MIT
/**
 * DialogProvider — global Toast + ConfirmDialog system.
 *
 * Usage:
 *   const { toast }   = useToast();
 *   const { confirm } = useConfirm();
 *
 *   toast.success("Saved!");
 *   toast.error("Failed to save");
 *   toast.info("Copied to clipboard");
 *   toast.warn("Please provide a reason");
 *
 *   const ok = await confirm({
 *     title:        "Delete Document",
 *     message:      "This will remove all embeddings. This cannot be undone.",
 *     confirmLabel: "Delete",      // default: "Confirm"
 *     variant:      "danger",      // "danger" | "primary"  default: "danger"
 *   });
 *   if (!ok) return;
 */

import { createContext, useContext, useState, useCallback, useRef } from "react";
import { X, AlertTriangle, CheckCircle, Info, AlertCircle } from "lucide-react";

// ─── Toast ────────────────────────────────────────────────────────────────────

const ToastContext = createContext(null);

function ToastIcon({ type }) {
  const cls = "w-4 h-4 shrink-0 mt-0.5";
  if (type === "success") return <CheckCircle  className={`${cls} text-green-500`} />;
  if (type === "error")   return <AlertCircle  className={`${cls} text-red-500`} />;
  if (type === "warn")    return <AlertTriangle className={`${cls} text-yellow-500`} />;
  return                         <Info          className={`${cls} text-blue-500`} />;
}

const TOAST_COLORS = {
  success: "bg-white border-green-400 text-gray-800",
  error:   "bg-white border-red-400   text-gray-800",
  warn:    "bg-white border-yellow-400 text-gray-800",
  info:    "bg-white border-blue-400  text-gray-800",
};

let _toastId = 0;

export function ToastProvider({ children }) {
  const [toasts, setToasts] = useState([]);

  const dismiss = useCallback((id) => {
    setToasts(t => t.filter(x => x.id !== id));
  }, []);

  const push = useCallback((type, message, duration = type === "error" ? 5000 : 3000) => {
    const id = ++_toastId;
    setToasts(t => [...t, { id, type, message }]);
    setTimeout(() => dismiss(id), duration);
  }, [dismiss]);

  const toast = {
    success: (msg) => push("success", msg),
    error:   (msg) => push("error",   msg),
    warn:    (msg) => push("warn",    msg),
    info:    (msg) => push("info",    msg),
  };

  return (
    <ToastContext.Provider value={toast}>
      {children}
      {/* Toast stack — top-right */}
      <div className="fixed top-4 right-4 z-[9999] flex flex-col gap-2 w-80 pointer-events-none">
        {toasts.map(t => (
          <div
            key={t.id}
            className={`flex items-start gap-2.5 px-3.5 py-3 rounded-lg border shadow-lg
                        pointer-events-auto animate-slide-in
                        ${TOAST_COLORS[t.type]}`}
          >
            <ToastIcon type={t.type} />
            <p className="flex-1 text-sm leading-snug">{t.message}</p>
            <button
              onClick={() => dismiss(t.id)}
              className="shrink-0 text-gray-400 hover:text-gray-600 transition-colors"
            >
              <X className="w-3.5 h-3.5" />
            </button>
          </div>
        ))}
      </div>
    </ToastContext.Provider>
  );
}

export function useToast() {
  const ctx = useContext(ToastContext);
  if (!ctx) throw new Error("useToast must be used inside <ToastProvider>");
  return { toast: ctx };
}

// ─── ConfirmDialog ────────────────────────────────────────────────────────────

const ConfirmContext = createContext(null);

export function ConfirmProvider({ children }) {
  const [dialog, setDialog] = useState(null);
  const resolveRef = useRef(null);

  const confirm = useCallback(({ title, message, confirmLabel = "Confirm", variant = "danger" }) => {
    return new Promise((resolve) => {
      resolveRef.current = resolve;
      setDialog({ title, message, confirmLabel, variant });
    });
  }, []);

  function handle(result) {
    setDialog(null);
    resolveRef.current?.(result);
    resolveRef.current = null;
  }

  const btnConfirm = dialog?.variant === "danger"
    ? "bg-red-600 hover:bg-red-700 text-white"
    : "bg-blue-600 hover:bg-blue-700 text-white";

  return (
    <ConfirmContext.Provider value={confirm}>
      {children}
      {dialog && (
        <div
          className="fixed inset-0 z-[9998] flex items-center justify-center bg-black/50 backdrop-blur-sm"
          onClick={() => handle(false)}
        >
          <div
            className="bg-white rounded-xl shadow-2xl w-full max-w-md mx-4 overflow-hidden"
            onClick={e => e.stopPropagation()}
          >
            {/* Header */}
            <div className="flex items-start justify-between px-5 pt-5 pb-3">
              <div className="flex items-center gap-3">
                {dialog.variant === "danger"
                  ? <AlertTriangle className="w-5 h-5 text-red-500 shrink-0" />
                  : <Info className="w-5 h-5 text-blue-500 shrink-0" />
                }
                <h3 className="font-semibold text-gray-900 text-base">{dialog.title}</h3>
              </div>
              <button
                onClick={() => handle(false)}
                className="cursor-pointer p-1 rounded-lg text-gray-400 hover:bg-gray-100 transition-colors ml-2"
              >
                <X className="w-4 h-4" />
              </button>
            </div>

            {/* Body — `whitespace-pre-line` so newline-separated lines
                (used by callers like KbChatList for scope/timestamp) render
                as multi-line bodies without HTML injection. */}
            <p className="px-5 pb-5 text-sm text-gray-600 leading-relaxed whitespace-pre-line">
              {dialog.message}
            </p>

            {/* Footer */}
            <div className="flex justify-end gap-2 px-5 py-3.5 bg-gray-50 border-t border-gray-100">
              <button
                onClick={() => handle(true)}
                className={`cursor-pointer px-4 py-2 text-sm font-medium rounded-lg transition-colors brand-grad hover:opacity-70 ${btnConfirm}`}
              >
                {dialog.confirmLabel}
              </button>
              <button
                onClick={() => handle(false)}
                className="cursor-pointer px-4 py-2 text-sm font-medium text-gray-700 bg-white border border-gray-300
                           rounded-lg hover:bg-gray-100 transition-colors"
              >
                Cancel
              </button>
              
            </div>
          </div>
        </div>
      )}
    </ConfirmContext.Provider>
  );
}

export function useConfirm() {
  const ctx = useContext(ConfirmContext);
  if (!ctx) throw new Error("useConfirm must be used inside <ConfirmProvider>");
  return { confirm: ctx };
}
