// SPDX-License-Identifier: Apache-2.0
export function Skeleton({ className = "" }) {
  return (
    <div
      className={`animate-pulse bg-gradient-to-r from-gray-200 via-gray-300 to-gray-200 bg-[length:200%_100%] ${className}`}
    />
  );
}

// Shared shimmer component for consistency
function ShimmerOverlay({ className = "" }) {
  return (
    <div
      className={`absolute inset-0 bg-gradient-to-r from-transparent via-white/40 to-transparent animate-shimmer pointer-events-none ${className}`}
    />
  );
}

// AI Avatar - matches left panel icon style
function AIAvatar({ size = "md", isAnimating = false }) {
  const sizeClasses = {
    sm: "w-7 h-7",
    md: "w-8 h-8",
    lg: "w-10 h-10",
  };

  return (
    <div className={`relative flex-shrink-0 ${sizeClasses[size]}`}>
      <div
        className={`relative ${sizeClasses[size]} rounded-lg bg-indigo-50 border border-indigo-100 flex items-center justify-center`}
      >
        <div className="w-4 h-4 rounded bg-indigo-500" />
      </div>
      {isAnimating && (
        <div className="absolute -bottom-1 left-1/2 -translate-x-1/2 flex gap-0.5">
          <span className="w-1 h-1 bg-indigo-500 rounded-full animate-bounce" style={{ animationDelay: "0ms" }} />
          <span className="w-1 h-1 bg-indigo-500 rounded-full animate-bounce" style={{ animationDelay: "150ms" }} />
          <span className="w-1 h-1 bg-indigo-500 rounded-full animate-bounce" style={{ animationDelay: "300ms" }} />
        </div>
      )}
    </div>
  );
}

// User Avatar - consistent across both panels
function UserAvatar({ size = "md" }) {
  const sizeClasses = {
    sm: "w-7 h-7",
    md: "w-8 h-8",
    lg: "w-10 h-10",
  };

  return (
    <div
      className={`relative ${sizeClasses[size]} rounded-full bg-gray-100 border border-gray-200 flex items-center justify-center`}
    >
      <div className={`${size === "sm" ? "w-3 h-3" : size === "md" ? "w-3.5 h-3.5" : "w-4 h-4"} bg-gray-400 rounded-full`} />
    </div>
  );
}

// Skeleton Line - consistent styling
function SkeletonLine({ width, height = "h-3", className = "", delay = 0 }) {
  return (
    <div
      className={`${width} ${height} rounded-full bg-gray-200 relative overflow-hidden ${className}`}
      style={{ animationDelay: `${delay}ms` }}
    >
      <ShimmerOverlay />
    </div>
  );
}

// Chat List Item Skeleton
function ChatListItemSkeleton({ index }) {
  return (
    <div
      className="group flex items-center gap-3 px-3 py-3 rounded-xl transition-all"
      style={{ animationDelay: `${index * 60}ms` }}
    >
      {/* Icon */}
      <div className="relative flex-shrink-0">
        <div className="w-9 h-9 rounded-lg bg-indigo-50 border border-indigo-100 flex items-center justify-center">
          <MessageSquareIcon />
        </div>
      </div>

      {/* Content */}
      <div className="flex-1 min-w-0 space-y-2">
        <SkeletonLine width="w-full" height="h-3" delay={index * 60} />
        <div className="flex items-center gap-2">
          <SkeletonLine width="w-5/6" height="h-2.5" className="bg-gray-100" delay={index * 60 + 20} />
          <div className="h-2 w-14 rounded-full bg-gray-100 relative overflow-hidden">
            <ShimmerOverlay className="via-white/20" />
          </div>
        </div>
      </div>

      {/* Actions */}
      <div className="flex items-center gap-1 opacity-0 group-hover:opacity-100 transition-opacity">
        <div className="w-7 h-7 rounded-md bg-gray-100" />
      </div>
    </div>
  );
}

// Message Square Icon for chat list
function MessageSquareIcon() {
  return (
    <div className="w-4 h-4 rounded bg-indigo-500" />
  );
}

// ==================== EXPORTED COMPONENTS ====================

export function ChatListSkeleton() {
  return (
    <div className="flex flex-col py-3 px-2">
      {[...Array(9)].map((_, i) => (
        <ChatListItemSkeleton key={i} index={i} />
      ))}
    </div>
  );
}

export function ChatMessageSkeleton() {
  const conversation = [
    {
      type: "assistant",
      content: [
        { kind: "text", lines: ["w-11/12", "w-4/5", "w-3/4"] },
        { kind: "code", lines: 4 },
        { kind: "text", lines: ["w-2/3"] },
      ],
      meta: { model: "Claude Sonnet", time: "2.4s" },
    },
    {
      type: "user",
      content: [{ kind: "text", lines: ["w-full", "w-5/6"] }],
      status: "sent",
    },
    {
      type: "assistant",
      content: [
        { kind: "text", lines: ["w-full", "w-11/12", "w-4/5"] },
        { kind: "list", items: 3 },
        { kind: "text", lines: ["w-3/4", "w-full"] },
      ],
      meta: { model: "GPT-5.2", time: "1.8s" },
    },
    {
      type: "user",
      content: [{ kind: "text", lines: ["w-4/5", "w-full", "w-3/4"] }],
      status: "read",
    },
    {
      type: "assistant",
      content: [{ kind: "text", lines: ["w-full", "w-5/6"] }],
      isTyping: true,
      meta: { model: "Claude Sonnet", time: "0.8s" },
    },
  ];

  return (
    <div className="flex flex-col gap-1 py-4">
      {conversation.map((msg, idx) => (
        <div
          key={idx}
          className={`flex ${msg.type === "user" ? "justify-end" : "justify-start"} gap-3 px-2 py-2`}
          style={{ animationDelay: `${idx * 100}ms` }}
        >
          {/* Avatar */}
          {msg.type === "assistant" ? (
            <div className="flex flex-col items-center gap-1">
              <AIAvatar size="md" isAnimating={msg.isTyping} />
            </div>
          ) : (
            <UserAvatar size="md" />
          )}

          {/* Message Container */}
          <div className={`flex flex-col ${msg.type === "user" ? "items-end" : "items-start"} max-w-3xl gap-1`}>
            {/* Bubble */}
            <div
              className={`relative overflow-hidden rounded-2xl shadow-sm ${
                msg.type === "user"
                  ? "bg-indigo-300 border border-indigo-200"
                  : "bg-white border border-gray-100"
              }`}
            >
              <ShimmerOverlay className={msg.type === "user" ? "via-white/20" : ""} />

              <div className="px-4 py-3.5 min-w-[200px] max-w-[600px]">
                {/* Content Sections */}
                <div className="space-y-3">
                  {msg.content.map((section, sIdx) => (
                    <div key={sIdx}>
                      {section.kind === "text" && (
                        <div className="space-y-2">
                          {section.lines.map((width, lIdx) => (
                            <SkeletonLine
                              key={lIdx}
                              width={width}
                              className={msg.type === "user" ? "bg-white/30" : "bg-gray-200"}
                              delay={idx * 80 + lIdx * 30}
                            />
                          ))}
                        </div>
                      )}

                      {section.kind === "code" && (
                        <div
                          className={`mt-2 rounded-lg p-3 ${
                            msg.type === "user" ? "bg-black/20" : "bg-gray-50 border border-gray-100"
                          }`}
                        >
                          {/* Window controls */}
                          <div className="flex items-center gap-1.5 mb-2.5">
                            <div className={`w-2.5 h-2.5 rounded-full ${msg.type === "user" ? "bg-white/30" : "bg-red-300"}`} />
                            <div className={`w-2.5 h-2.5 rounded-full ${msg.type === "user" ? "bg-white/30" : "bg-amber-300"}`} />
                            <div className={`w-2.5 h-2.5 rounded-full ${msg.type === "user" ? "bg-white/30" : "bg-green-300"}`} />
                            <div
                              className={`ml-2 h-2 w-20 rounded ${msg.type === "user" ? "bg-white/20" : "bg-gray-200"}`}
                            />
                          </div>
                          {/* Code lines */}
                          <div className="space-y-1.5">
                            {[...Array(section.lines)].map((_, cIdx) => (
                              <div
                                key={cIdx}
                                className={`h-2 rounded ${msg.type === "user" ? "bg-white/20" : "bg-gray-200"} ${
                                  cIdx === section.lines - 1 ? "w-2/3" : "w-full"
                                }`}
                              />
                            ))}
                          </div>
                        </div>
                      )}

                      {section.kind === "list" && (
                        <div className="space-y-2 mt-1">
                          {[...Array(section.items)].map((_, itemIdx) => (
                            <div key={itemIdx} className="flex items-start gap-2.5">
                              <div
                                className={`w-1.5 h-1.5 rounded-full mt-1.5 ${
                                  msg.type === "user" ? "bg-white/40" : "bg-indigo-300"
                                }`}
                              />
                              <SkeletonLine
                                width="flex-1"
                                height="h-2.5"
                                className={msg.type === "user" ? "bg-white/25" : "bg-gray-200"}
                                delay={idx * 80 + itemIdx * 20}
                              />
                            </div>
                          ))}
                        </div>
                      )}
                    </div>
                  ))}
                </div>

                {/* Typing Indicator */}
                {msg.isTyping && (
                  <div className="flex items-center gap-2 mt-3 pt-2 border-t border-gray-100/50">
                    <span className="text-xs text-indigo-500 font-medium">AiNxt is thinking</span>
                    <div className="flex gap-0.5">
                      <span className="w-1 h-1 bg-indigo-400 rounded-full animate-bounce" style={{ animationDelay: "0ms" }} />
                      <span className="w-1 h-1 bg-indigo-400 rounded-full animate-bounce" style={{ animationDelay: "150ms" }} />
                      <span className="w-1 h-1 bg-indigo-400 rounded-full animate-bounce" style={{ animationDelay: "300ms" }} />
                    </div>
                  </div>
                )}
              </div>
            </div>

            {/* Meta Row */}
            {msg.meta && (
              <div className="flex items-center gap-2 px-1 mt-0.5">
                <div className="flex items-center gap-1.5">
                  <div className="w-2 h-2 rounded-full bg-gradient-to-r from-indigo-400 to-purple-500" />
                  <div className="h-2 w-20 rounded-full bg-gray-100 relative overflow-hidden">
                    <ShimmerOverlay className="via-white/20" />
                  </div>
                </div>
                <span className="text-gray-300">·</span>
                <div className="h-2 w-10 rounded-full bg-gray-100" />
              </div>
            )}

            {/* User Status */}
            {msg.status && (
              <div className="flex items-center gap-1.5 px-1">
                <div className="h-2 w-12 rounded-full bg-gray-100" />
                {msg.status === "read" && <div className="w-3 h-3 rounded-full bg-indigo-400" />}
              </div>
            )}
          </div>
        </div>
      ))}

      {/* Loading Indicator */}
      <div className="flex justify-center pt-6 pb-2">
        <div className="flex items-center gap-2 px-4 py-2.5 bg-gradient-to-r from-indigo-50 to-purple-50 rounded-full border border-indigo-100 shadow-sm">
          <div className="relative">
            <div className="w-2 h-2 bg-indigo-500 rounded-full animate-pulse" />
            <div className="absolute inset-0 w-2 h-2 bg-indigo-500 rounded-full animate-ping opacity-30" />
          </div>
          <span className="text-sm text-indigo-600 font-medium">Loading conversation history...</span>
        </div>
      </div>
    </div>
  );
}

// Streaming Message Skeleton - shown while AI is generating response
export function StreamingMessageSkeleton() {
  return (
    <div className="flex gap-3 py-2">
      {/* AI Avatar */}
      <div className="flex flex-col items-center gap-1">
        <AIAvatar size="md" isAnimating />
      </div>

      {/* Message Bubble */}
      <div className="flex flex-col items-start max-w-2xl gap-1">
        <div className="relative overflow-hidden rounded-2xl bg-white border border-gray-100 shadow-sm">
          <ShimmerOverlay />
          <div className="px-4 py-3.5 min-w-[280px]">
            <div className="space-y-2.5">
              <SkeletonLine width="w-full" height="h-3" delay={0} />
              <SkeletonLine width="w-5/6" height="h-3" delay={50} />
              <SkeletonLine width="w-4/5" height="h-3" delay={100} />
            </div>

            {/* Typing Indicator */}
            <div className="flex items-center gap-2 mt-3 pt-2 border-t border-gray-100">
              <span className="text-xs text-indigo-500 font-medium">AiNxt is thinking</span>
              <div className="flex gap-0.5">
                <span className="w-1 h-1 bg-indigo-400 rounded-full animate-bounce" style={{ animationDelay: "0ms" }} />
                <span className="w-1 h-1 bg-indigo-400 rounded-full animate-bounce" style={{ animationDelay: "150ms" }} />
                <span className="w-1 h-1 bg-indigo-400 rounded-full animate-bounce" style={{ animationDelay: "300ms" }} />
              </div>
            </div>
          </div>
        </div>

        {/* Meta row */}
        <div className="flex items-center gap-2 px-1 mt-0.5">
          <div className="flex items-center gap-1.5">
            <div className="w-2 h-2 rounded-full bg-indigo-400 animate-pulse" />
            <div className="h-2 w-24 rounded-full bg-gray-100 relative overflow-hidden">
              <ShimmerOverlay className="via-white/20" />
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}

export default Skeleton;
