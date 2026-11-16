import { useEffect, useState, useSyncExternalStore } from "react";
import { isGame } from "./contracts";
import { SessionController } from "./session";
import { Home, Lobby, Game } from "./screens";
import { ConfirmDialog, type Confirmation } from "./components/Dialog";

export function App({ controller }: { controller: SessionController }) {
  const state = useSyncExternalStore(
    controller.subscribe,
    controller.getSnapshot,
  );
  const [confirmation, setConfirmation] = useState<Confirmation | null>(null);
  useEffect(() => {
    void controller.bootstrap();
    const pop = () => {
      setConfirmation(null);
      controller.navigate();
    };
    window.addEventListener("popstate", pop);
    return () => window.removeEventListener("popstate", pop);
  }, [controller]);
  useEffect(() => {
    setConfirmation(null);
  }, [state.snapshot?.phase, state.sessionLost, state.room?.code]);
  const game = state.snapshot && isGame(state.snapshot) ? state.snapshot : null;
  const connectionLabel =
    state.connection === "connected"
      ? "На связи"
      : state.connection === "syncing"
        ? "Обновление…"
        : state.connection === "connecting"
          ? "Подключение…"
          : "Переподключение…";
  const notice = state.error || (state.pending && !state.busy);
  return (
    <div
      className={`app-shell ${notice ? "has-notice" : ""} ${game ? "is-game" : ""}`}
      aria-busy={state.busy || state.loading}
    >
      <header className="app-header">
        <a
          className="brand"
          href="/"
          onClick={(e) => {
            e.preventDefault();
            setConfirmation(null);
            controller.home();
          }}
          aria-label="Детектив — на главную"
        >
          <span className="brand-mark" aria-hidden="true">
            Д
          </span>
          <span>
            Детектив<small>совместное расследование</small>
          </span>
        </a>
        <div className="header-right">
          {state.room && (
            <>
              <span className="room-label">Комната {state.room.code}</span>
              <span
                className={`connection ${state.connection === "connected" ? "online" : ""}`}
                role="status"
                data-testid="connection-status"
              >
                {connectionLabel}
              </span>
              {state.connection === "reconnecting" && (
                <button
                  className="text-button"
                  onClick={() => controller.reconnect()}
                >
                  Повторить подключение
                </button>
              )}
            </>
          )}
          {state.loading && <span role="status">Загрузка…</span>}
        </div>
      </header>
      {notice && (
        <div className="notices">
          <div className="notice-content">
            {state.error && <p role="alert">{state.error}</p>}
            {state.pending && (
              <p>
                Запрос сохранён. Пока его результат не подтверждён, новые
                игровые действия недоступны.
              </p>
            )}
          </div>
          {state.pending && (
            <button
              disabled={state.busy || state.connection !== "connected"}
              onClick={() => void controller.retryPending()}
            >
              Повторить сохранённый запрос
            </button>
          )}
          {state.error && !state.sessionLost && (
            <button
              className="text-button"
              aria-label="Закрыть сообщение"
              onClick={() => controller.dismissError()}
            >
              Закрыть
            </button>
          )}
        </div>
      )}
      {state.loading && !state.room ? (
        <main className="loading-screen">
          <span className="eyebrow">Открываем дело</span>
          <h1>Восстанавливаем вашу сессию…</h1>
        </main>
      ) : !state.room ? (
        <Home state={state} controller={controller} />
      ) : !state.snapshot ? (
        <main className="loading-screen">
          <h1>
            {state.error
              ? "Не удалось открыть комнату"
              : "Получаем состояние комнаты…"}
          </h1>
          <button className="secondary" onClick={() => controller.home()}>
            На главную
          </button>
        </main>
      ) : game ? (
        <Game
          game={game}
          state={state}
          controller={controller}
          confirm={setConfirmation}
        />
      ) : (
        <Lobby state={state} controller={controller} />
      )}
      {confirmation && (
        <ConfirmDialog
          value={confirmation}
          close={() => setConfirmation(null)}
          disabled={
            state.busy ||
            state.pending ||
            !["connected", "syncing"].includes(state.connection)
          }
        />
      )}
    </div>
  );
}
