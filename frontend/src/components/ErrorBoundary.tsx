import { Component, type ErrorInfo, type ReactNode } from "react";

/** Keeps one failing panel from unmounting the whole editor (and stopping playback). */
export class ErrorBoundary extends Component<{ name: string; children: ReactNode }, { error: Error | null }> {
  state = { error: null as Error | null };

  static getDerivedStateFromError(error: Error) {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error(`[${this.props.name}]`, error, info.componentStack);
  }

  render() {
    if (!this.state.error) return this.props.children;
    return (
      <div className="flex h-full flex-col items-center justify-center gap-2 bg-red-50 p-4 text-sm text-red-800">
        <p>{this.props.name} 出错：{this.state.error.message}</p>
        <button className="rounded border border-red-300 bg-white px-2 py-1" onClick={() => this.setState({ error: null })}>
          重试
        </button>
      </div>
    );
  }
}
