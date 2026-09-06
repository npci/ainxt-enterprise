// SPDX-License-Identifier: MIT
import { Component } from "react";

/**
 * Catches render-time JavaScript errors in any child component.
 * Without this, any crash = blank white page with no feedback.
 */
export default class ErrorBoundary extends Component {
  constructor(props) {
    super(props);
    this.state = { error: null };
  }

  static getDerivedStateFromError(error) {
    return { error };
  }

  componentDidCatch(error, info) {
    console.error("[ErrorBoundary] Caught render error:", error, info);
  }

  render() {
    if (this.state.error) {
      return (
        <div className="flex flex-col items-center justify-center h-full gap-4 p-8 text-center">
          <div className="text-red-500 text-lg font-semibold">Something went wrong</div>
          <pre className="text-xs text-left bg-red-50 border border-red-200 rounded p-4 max-w-xl overflow-auto text-red-700 whitespace-pre-wrap">
            {this.state.error.message}
            {"\n\n"}
            {this.state.error.stack}
          </pre>
          <button
            onClick={() => this.setState({ error: null })}
            className="px-4 py-2 text-sm bg-gray-900 text-white rounded hover:bg-gray-700"
          >
            Try again
          </button>
        </div>
      );
    }
    return this.props.children;
  }
}
