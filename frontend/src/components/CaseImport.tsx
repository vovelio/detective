import { useState, type FormEvent } from "react";
import { importIssueLabels } from "../api";
import type { AppState, SessionController } from "../session";

const MAX_FILE_BYTES = 20 * 1024 * 1024;
export function CaseImport({
  state,
  controller,
  disabled,
}: {
  state: AppState;
  controller: SessionController;
  disabled: boolean;
}) {
  const [open, setOpen] = useState(false);
  const [file, setFile] = useState<File | null>(null);
  const [localError, setLocalError] = useState("");
  const status = state.importing;
  const uploading = status.status === "uploading";
  function submit(event: FormEvent) {
    event.preventDefault();
    if (!file || disabled) return;
    if (!file.name.toLowerCase().endsWith(".detective")) {
      setLocalError(
        "Выберите файл с расширением .detective — ZIP-пакет с case.json и ресурсами.",
      );
      return;
    }
    if (file.size > MAX_FILE_BYTES) {
      setLocalError("Размер файла не должен превышать 20 МиБ.");
      return;
    }
    setLocalError("");
    void controller.importCase(file);
  }
  return (
    <section className="case-import" aria-label="Импорт своего дела">
      <button
        type="button"
        className="secondary"
        data-testid="import-open"
        disabled={disabled}
        onClick={() => {
          setOpen(true);
          setLocalError("");
          controller.resetImport();
        }}
      >
        Добавить свою игру
      </button>
      {!open && (
        <small>
          Готовый файл .detective, до 20 МиБ. Добавленное дело доступно только
          этой комнате.
        </small>
      )}
      {open && (
        <form data-testid="import-form" onSubmit={submit} aria-busy={uploading}>
          <label htmlFor="detective-file">Файл детективного дела</label>
          <input
            id="detective-file"
            data-testid="import-file"
            type="file"
            accept=".detective"
            disabled={disabled}
            onChange={(e) => {
              setFile(e.target.files?.[0] ?? null);
              setLocalError("");
              controller.resetImport();
            }}
          />
          <small>
            Нужен готовый ZIP-пакет .detective с case.json версии 1.0.0 и всеми
            изображениями. Сервер проверит структуру и безопасность; качество
            истории проверяет автор.
          </small>
          {localError && <p role="alert">{localError}</p>}
          {status.status !== "idle" && (
            <div
              data-testid="import-state"
              data-status={status.status}
              role={
                status.status === "error" || status.status === "uncertain"
                  ? "alert"
                  : "status"
              }
            >
              <p>{status.message}</p>
              {!!status.issues.length && (
                <ul data-testid="import-issues">
                  {status.issues.map((issue, index) => (
                    <li key={index}>
                      <code>{issue.path}</code>:{" "}
                      {importIssueLabels[issue.code] ??
                        importIssueLabels.VALIDATION_ERROR}
                    </li>
                  ))}
                </ul>
              )}
            </div>
          )}
          <div className="import-actions">
            <button
              type="submit"
              data-testid="import-submit"
              disabled={disabled || !file}
            >
              {uploading ? "Загрузка и проверка…" : "Загрузить дело"}
            </button>
            <button
              type="button"
              data-testid="import-refresh"
              className="secondary"
              disabled={disabled}
              onClick={() => void controller.refreshCases()}
            >
              Обновить список дел
            </button>
            <button
              type="button"
              data-testid="import-close"
              className="text-button"
              disabled={uploading}
              onClick={() => setOpen(false)}
            >
              Закрыть
            </button>
          </div>
        </form>
      )}
    </section>
  );
}
