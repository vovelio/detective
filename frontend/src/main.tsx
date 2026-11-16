import { Component, type ErrorInfo, type ReactNode } from "react";
import { createRoot } from "react-dom/client";
import { App } from "./App";
import { SessionController } from "./session";
import "./styles.css";

class Boundary extends Component<{ children: ReactNode }, { failed: boolean }> {
  state = { failed: false };
  static getDerivedStateFromError() {
    return { failed: true };
  }
  componentDidCatch(_error: Error, _info: ErrorInfo) {
    /* Do not log session data or component props. */
  }
  render() {
    return this.state.failed ? (
      <main className="loading-screen">
        <h1>Не удалось показать страницу</h1>
        <p>Обновите её, чтобы получить актуальное состояние с сервера.</p>
        <button onClick={() => location.reload()}>Обновить страницу</button>
      </main>
    ) : (
      this.props.children
    );
  }
}
const root = document.getElementById("root");
if (!root) throw new Error("Application root is missing");
createRoot(root).render(
  <Boundary>
    <App controller={new SessionController()} />
  </Boundary>,
);
