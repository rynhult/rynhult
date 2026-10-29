import { Component, type ErrorInfo, type ReactNode } from "react";
import { Button, Card, Page } from "./ui";

/** One page failing to render (e.g. odd content an agent wrote) must not blank the whole dashboard. Resets when
    the route changes (`resetKey`). */
export class ErrorBoundary extends Component<{ resetKey: string; children: ReactNode; reloadOnRetry?: boolean }, { error: Error | null }> {
  state = { error: null as Error | null };

  static getDerivedStateFromError(error: Error) { return { error }; }

  componentDidCatch(error: Error, info: ErrorInfo) { console.error("page failed to render", error, info.componentStack); }

  componentDidUpdate(previous: { resetKey: string }) {
    if (previous.resetKey !== this.props.resetKey && this.state.error) this.setState({ error: null });
  }

  render() {
    if (!this.state.error) return this.props.children;
    return (
      <Page>
        <Card className="p-6">
          <h1 className="text-[15px] font-semibold">This page couldn't be shown</h1>
          <p className="mt-1 text-[13px] text-muted">Something on it failed to render. The rest of Rynhult still works.</p>
          <pre tabIndex={0} role="region" aria-label="Error details" className="mt-3 overflow-x-auto rounded-md bg-panel-2 p-3 font-mono text-xs text-faint">{this.state.error.message}</pre>
          <Button className="mt-4" onClick={() => this.props.reloadOnRetry ? window.location.reload() : this.setState({ error: null })}>{this.props.reloadOnRetry ? "Reload page" : "Try again"}</Button>
        </Card>
      </Page>
    );
  }
}
