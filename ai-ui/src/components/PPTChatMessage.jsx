// SPDX-License-Identifier: MIT
// PPTChatMessage.jsx
// Minimal rich cards for PPT generation - outlines shown as chat text
// Editing handled via natural language prompts

import {
  Loader2,
  Download,
  RefreshCw,
} from 'lucide-react';

// ── PPT Progress Message ────────────────────────────────────────────────────

export function PPTProgressMessage({ progress }) {
  return (
    <div className="flex flex-col gap-2">
      <div className="flex items-center gap-2">
        <Loader2 className="w-4 h-4 animate-spin text-indigo-500" />
        <p className="text-sm text-gray-700">
          Generating your presentation...
        </p>
      </div>
      <div className="w-full max-w-xs h-2 bg-gray-200 rounded-full overflow-hidden">
        <div
          className="h-full brand-grad-r transition-all duration-500"
          style={{ width: `${Math.min(progress, 90)}%` }}
        />
      </div>
      <p className="text-xs text-gray-500">{Math.round(progress)}% complete</p>
    </div>
  );
}

// ── PPT Complete Message ────────────────────────────────────────────────────

export function PPTCompleteMessage({
  title,
  format,
  onDownload,
  downloading,
}) {
  return (
    <div className="flex flex-col gap-2">
      <p className="text-sm text-gray-700">
        ✅ Your presentation is ready!
      </p>
      
      <button
        onClick={onDownload}
        disabled={downloading}
        className="w-fit flex items-center gap-2 px-5 py-2.5 brand-grad hover:opacity-90 disabled:opacity-50 text-white text-sm font-medium rounded-lg transition shadow-sm"
      >
        {downloading ? (
          <>
            <Loader2 className="w-4 h-4 animate-spin" />
            Downloading...
          </>
        ) : (
          <>
            <Download className="w-4 h-4" />
            Download {title || 'Presentation'}.{(format || 'pptx').toLowerCase()}
          </>
        )}
      </button>
    </div>
  );
}

// ── PPT Error Message ───────────────────────────────────────────────────────

export function PPTErrorMessage({ error, onRetry }) {
  return (
    <div className="flex flex-col gap-2">
      <p className="text-sm text-red-600">
        ❌ Generation failed: {error}
      </p>
      <button
        onClick={onRetry}
        className="w-fit flex items-center gap-1.5 px-4 py-2 bg-red-100 hover:bg-red-200 text-red-700 text-sm font-medium rounded-lg transition"
      >
        <RefreshCw className="w-4 h-4" />
        Try Again
      </button>
    </div>
  );
}
