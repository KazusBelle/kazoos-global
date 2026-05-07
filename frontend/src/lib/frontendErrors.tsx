import React from "react";

import { reportFrontendError } from "./api";

type ChartContext = {
  symbol?: string;
  interval?: string;
  tab?: string;
  theme?: string;
  editMode?: boolean;
} | null;

declare global {
  interface Window {
    __kazusChartContext?: ChartContext;
    __kazusFrontendReporterInstalled?: boolean;
  }
}

function currentContext(): ChartContext {
  return typeof window === "undefined" ? null : (window.__kazusChartContext ?? null);
}

function normalizeErrorPayload(kind: string, message: string, stack?: string | null, source?: string | null) {
  return {
    kind,
    message: message || "unknown frontend error",
    source: source ?? undefined,
    stack: stack ?? undefined,
    url: typeof window !== "undefined" ? window.location.href : undefined,
    user_agent: typeof navigator !== "undefined" ? navigator.userAgent : undefined,
    context: currentContext() ?? undefined,
  };
}

export function installFrontendErrorReporter() {
  if (typeof window === "undefined" || window.__kazusFrontendReporterInstalled) return;
  window.__kazusFrontendReporterInstalled = true;

  window.addEventListener("error", (event) => {
    const err = event.error as Error | undefined;
    void reportFrontendError(
      normalizeErrorPayload(
        "window.error",
        err?.message || event.message || "window error",
        err?.stack || null,
        event.filename || null,
      ),
    );
  });

  window.addEventListener("unhandledrejection", (event) => {
    const reason = event.reason;
    let message = "unhandled rejection";
    let stack: string | null = null;
    if (reason instanceof Error) {
      message = reason.message || message;
      stack = reason.stack || null;
    } else if (typeof reason === "string") {
      message = reason;
    } else {
      try {
        message = JSON.stringify(reason);
      } catch {
        message = String(reason);
      }
    }
    void reportFrontendError(
      normalizeErrorPayload("unhandledrejection", message, stack, "promise"),
    );
  });
}

export class FrontendErrorBoundary extends React.Component<
  { children: React.ReactNode },
  { crashed: boolean }
> {
  state = { crashed: false };

  static getDerivedStateFromError() {
    return { crashed: true };
  }

  componentDidCatch(error: Error, info: React.ErrorInfo) {
    void reportFrontendError(
      normalizeErrorPayload(
        "react.error_boundary",
        error.message || "react render error",
        [error.stack, info.componentStack].filter(Boolean).join("\n"),
        "react",
      ),
    );
  }

  render() {
    if (this.state.crashed) {
      return (
        <div className="min-h-screen flex items-center justify-center bg-[#111113] text-[#f4f4f5]">
          <div className="text-sm font-mono opacity-80">Interface crashed. Reload the page.</div>
        </div>
      );
    }
    return this.props.children;
  }
}
