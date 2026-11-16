import type {
  ActionBody,
  ImportIssue,
  ImportReceipt,
  CaseInfo,
  Pending,
  Receipt,
  RoomInfo,
  SessionInfo,
  Snapshot,
} from "./contracts.ts";

const messages: Record<string, string> = {
  RATE_LIMITED:
    "Слишком много запросов. Подождите немного и повторите действие.",
  REQUEST_TIMEOUT:
    "Не удалось вовремя передать запрос. Проверьте соединение и повторите действие.",
  HTTPS_REQUIRED: "Для этого сервера требуется защищённое HTTPS-соединение.",
  HOST_DENIED: "Адрес сайта не соответствует настройкам сервера.",
  CASE_VALIDATION_FAILED:
    "Файл не прошёл проверку. Исправьте указанные ошибки и загрузите его снова.",
  CASE_ID_CONFLICT:
    "В этой комнате уже есть дело с таким идентификатором и другим содержимым. Измените id нового дела или используйте прежний файл.",
  IMPORT_TOO_LARGE: "Файл превышает допустимый размер 20 МиБ.",
  IMPORT_TIMEOUT:
    "Проверка файла не завершилась вовремя. Повторите загрузку позднее.",
  IMPORT_BUSY:
    "Сервер уже проверяет другие файлы. Повторите загрузку немного позже.",
  IMPORT_LOBBY_ONLY: "Добавить дело можно только до начала расследования.",
  INVALID_MULTIPART:
    "Не удалось прочитать загрузку. Выберите один файл .detective и повторите.",
  CASE_STORAGE_UNAVAILABLE:
    "Хранилище дел временно недоступно. Обновите список перед повторной загрузкой.",
  CASE_LIMIT: "В комнате достигнут лимит добавленных дел.",
  BOARD_VERSION_CONFLICT:
    "Этот объект уже изменён другим участником. Загружена актуальная версия; ваше действие не сохранено. Повторите его после проверки.",
  BOARD_TARGET_INVALID: "Объект доски недоступен. Обновите состояние.",
  BOARD_PHASE_INVALID: "Редактирование доски доступно во время расследования.",
  BOARD_LIMIT: "Достигнут технический лимит доски или аннотации.",
  BOARD_OBJECT_EXISTS:
    "Объект с таким идентификатором уже существует. Обновите состояние.",
  SESSION_REQUIRED:
    "Сессия не найдена. Возврат на прежнее место без cookie невозможен.",
  SESSION_EXPIRED_OR_INVALID:
    "Сессия истекла или недействительна. Для нового входа удалите данные сайта в настройках браузера. Прежнее место автоматически не восстанавливается.",
  NOT_A_MEMBER:
    "У этой сессии нет доступа к комнате. Если cookie удалена, прежнее место восстановить автоматически нельзя.",
  HOST_ONLY: "Это действие доступно только ведущему.",
  ROOM_NOT_FOUND: "Комната не найдена. Проверьте ссылку или код.",
  NICKNAME_TAKEN: "Это имя уже занято в комнате.",
  ROOM_FULL: "В комнате уже восемь участников.",
  GAME_ALREADY_STARTED:
    "Расследование уже началось. Войти может только прежний участник с сохранённой cookie.",
  REVISION_CONFLICT:
    "Состояние изменилось. Загружена актуальная версия; проверьте её перед новым действием.",
  NOT_YOUR_TURN: "Сейчас ход другого участника.",
  CARD_NOT_IN_HAND: "Этой карты уже нет в вашей руке.",
  WRONG_PHASE: "Действие недоступно на текущем этапе расследования.",
  CSRF_FAILED:
    "Защита запроса устарела. Обновите страницу и повторите сохранённый запрос.",
  ORIGIN_DENIED:
    "Адрес интерфейса не разрешён сервером. Проверьте ALLOWED_ORIGINS.",
  INVALID_REQUEST: "Проверьте введённые данные.",
  INVALID_PLAYERS: "Для старта нужны от двух до восьми игроков.",
  INVALID_PLAYER_COUNT: "Число игроков не подходит для выбранного дела.",
  CASE_REQUIRED: "Сначала выберите дело.",
  ANSWERS_INCOMPLETE: "Команда должна выбрать вариант для каждого вопроса.",
  ACTION_ID_REUSED:
    "Идентификатор запроса уже использован. Запрос не был повторно применён.",
  DATABASE_UNAVAILABLE:
    "Сервер временно недоступен. Результат запроса пока не подтверждён.",
};
export class ApiError extends Error {
  code: string;
  status: number;
  issues: ImportIssue[];
  constructor(code: string, status: number, issues: ImportIssue[] = []) {
    super(
      messages[code] ??
        "Не удалось выполнить запрос. Обновите состояние и попробуйте ещё раз.",
    );
    this.code = code;
    this.status = status;
    this.issues = issues;
  }
}
export class NetworkError extends Error {
  constructor() {
    super(
      "Связь прервалась. Результат действия неизвестен; повторите сохранённый запрос с прежним идентификатором.",
    );
  }
}
export class ApiClient {
  private csrf: string | null = null;
  private transport: typeof fetch;
  constructor(transport: typeof fetch = (...args) => fetch(...args)) {
    this.transport = transport;
  }
  private async request<T>(
    path: string,
    body?: unknown,
    signal?: AbortSignal,
    timeoutMs = 15000,
  ): Promise<T> {
    const controller = new AbortController();
    const abort = () => controller.abort();
    signal?.addEventListener("abort", abort, { once: true });
    if (signal?.aborted) controller.abort();
    const timeout = setTimeout(abort, timeoutMs);
    const multipart = body instanceof FormData;
    try {
      const response = await this.transport(path, {
        method: body === undefined ? "GET" : "POST",
        credentials: "same-origin",
        cache: "no-store",
        signal: controller.signal,
        headers:
          body === undefined
            ? {}
            : {
                ...(!multipart ? { "Content-Type": "application/json" } : {}),
                ...(this.csrf ? { "X-CSRF-Token": this.csrf } : {}),
              },
        ...(body === undefined
          ? {}
          : { body: multipart ? body : JSON.stringify(body) }),
      });
      if (!response.ok) {
        let code = "REQUEST_FAILED";
        let issues: ImportIssue[] = [];
        try {
          const error: unknown = await response.json();
          if (typeof error === "object" && error !== null && "error" in error) {
            const data = error.error;
            if (
              typeof data === "object" &&
              data !== null &&
              "code" in data &&
              typeof data.code === "string"
            )
              code = data.code;
            if (typeof data === "object" && data !== null && "issues" in data)
              issues = safeImportIssues(data.issues);
          }
        } catch {
          /* Never echo an HTML proxy error or submitted body. */
        }
        throw new ApiError(code, response.status, issues);
      }
      return (await response.json()) as T;
    } catch (error) {
      if (error instanceof ApiError) throw error;
      if (signal?.aborted) throw new DOMException("Aborted", "AbortError");
      throw new NetworkError();
    } finally {
      clearTimeout(timeout);
      signal?.removeEventListener("abort", abort);
    }
  }
  async bootstrap(): Promise<SessionInfo> {
    const session = await this.request<SessionInfo>("/api/v1/sessions", {});
    this.csrf = session.csrf_token;
    return session;
  }
  cases(): Promise<CaseInfo[]> {
    return this.request("/api/v1/cases");
  }
  roomCases(code: string, signal?: AbortSignal): Promise<CaseInfo[]> {
    return this.request(
      `/api/v1/rooms/${encodeURIComponent(code)}/cases`,
      undefined,
      signal,
    );
  }
  importCase(
    code: string,
    file: File,
    signal?: AbortSignal,
  ): Promise<ImportReceipt> {
    const data = new FormData();
    data.append("file", file);
    return this.request(
      `/api/v1/rooms/${encodeURIComponent(code)}/cases/import`,
      data,
      signal,
      50000,
    );
  }
  create(nickname: string): Promise<RoomInfo> {
    return this.request("/api/v1/rooms", { nickname });
  }
  join(code: string, nickname: string): Promise<RoomInfo> {
    return this.request(`/api/v1/rooms/${encodeURIComponent(code)}/join`, {
      nickname,
    });
  }
  room(code: string, signal?: AbortSignal): Promise<RoomInfo> {
    return this.request(
      `/api/v1/rooms/${encodeURIComponent(code)}`,
      undefined,
      signal,
    );
  }
  snapshot(code: string, signal?: AbortSignal): Promise<Snapshot> {
    return this.request(
      `/api/v1/rooms/${encodeURIComponent(code)}/snapshot`,
      undefined,
      signal,
    );
  }
  select(code: string, caseId: string): Promise<RoomInfo> {
    return this.request(
      `/api/v1/rooms/${encodeURIComponent(code)}/cases/select`,
      { case_id: caseId },
    );
  }
  execute(pending: Pending): Promise<Receipt> {
    return this.request(
      `/api/v1/rooms/${encodeURIComponent(pending.code)}/${pending.kind === "start" ? "start" : "actions"}`,
      pending.body,
    );
  }
  clearCredentials(): void {
    this.csrf = null;
  }
}

export function parseRoom(input: string): string | null {
  const trimmed = input.trim();
  let code = trimmed;
  if (/^https?:\/\//.test(trimmed) || trimmed.startsWith("/rooms/")) {
    try {
      code =
        new URL(trimmed, "http://local.invalid").pathname.match(
          /^\/rooms\/([^/]+)\/?$/,
        )?.[1] ?? "";
    } catch {
      return null;
    }
  }
  return /^[A-Za-z0-9_-]{8,24}$/.test(code) ? code : null;
}
export function validNickname(input: string): boolean {
  const name = input.trim().normalize("NFC");
  return (
    Array.from(name).length >= 2 &&
    Array.from(name).length <= 20 &&
    !/\p{C}/u.test(name)
  );
}
const actionNames = new Set<ActionBody["type"]>([
  "board_mutation",
  "begin_play",
  "play_card",
  "propose_finish",
  "confirm_finish",
  "answer",
  "submit_answers",
  "review_answer",
  "finish_game",
]);
export function restorePending(raw: string | null): Pending | null {
  try {
    const value = JSON.parse(raw ?? "null") as Partial<Pending> | null;
    if (
      !value ||
      typeof value.code !== "string" ||
      !parseRoom(value.code) ||
      typeof value.memberId !== "string" ||
      !value.body
    )
      return null;
    const body = value.body;
    if (
      !/^[a-f0-9-]{36}$/i.test(value.memberId) ||
      !/^[a-f0-9-]{36}$/i.test(body.client_action_id) ||
      !Number.isInteger(body.expected_revision) ||
      body.expected_revision < 0
    )
      return null;
    if (value.kind === "start") return value as Pending;
    if (
      value.kind === "action" &&
      "type" in body &&
      actionNames.has(body.type) &&
      typeof body.payload === "object" &&
      body.payload !== null
    )
      return value as Pending;
  } catch {
    /* An invalid browser intent is never sent. */
  }
  return null;
}

export const importIssueLabels: Record<string, string> = {
  SCHEMA_INVALID: "Структура или значение поля не соответствует формату 1.0.0",
  DUPLICATE: "Повторяющийся идентификатор или ресурс",
  UNKNOWN_OPTION: "Указан несуществующий вариант ответа",
  UNKNOWN_CARD: "Указана несуществующая карточка",
  UNKNOWN_QUESTION: "Указан несуществующий вопрос",
  UNKNOWN_ASSET: "Указан несуществующий ресурс",
  INVALID_PLAYER_RANGE: "Неверный диапазон числа игроков",
  INSUFFICIENT_CARDS: "Недостаточно карт для заявленного числа игроков",
  INSUFFICIENT_RED_HERRINGS:
    "Недостаточно нерелевантных карт для согласованных правил",
  DUPLICATE_JSON_KEY: "В JSON повторяется имя поля",
  INVALID_JSON_CONSTANT: "JSON содержит недопустимое числовое значение",
  INVALID_JSON: "Файл case.json повреждён или имеет неверную кодировку",
  INVALID_IMAGE:
    "Изображение повреждено или не соответствует разрешённому формату",
  ANIMATED_IMAGE_NOT_ALLOWED: "Анимированные изображения не поддерживаются",
  ZIP_TOO_LARGE: "Архив превышает допустимый размер",
  INVALID_ZIP: "Не удалось прочитать ZIP-архив",
  TOO_MANY_FILES: "В архиве слишком много файлов",
  UNPACKED_TOO_LARGE: "Распакованные данные превышают допустимый объём",
  UNSAFE_OR_DUPLICATE_PATH: "Небезопасный или повторяющийся путь в архиве",
  NON_REGULAR_FILE:
    "Архив содержит папку, ссылку или неподдерживаемый тип файла",
  UNSUPPORTED_ZIP_ENTRY: "Неподдерживаемое сжатие или зашифрованная запись",
  MISSING_MANIFEST: "В архиве отсутствует case.json",
  MISSING_OR_UNDECLARED_FILE:
    "Отсутствует объявленный ресурс или есть лишний файл",
  MIME_EXTENSION_MISMATCH:
    "Расширение изображения не соответствует его формату",
  VALIDATION_ERROR: "Дело не соответствует требованиям формата",
};
const safePathNames = new Set([
  "case.json",
  "assets",
  "format",
  "format_version",
  "id",
  "meta",
  "title",
  "author",
  "summary",
  "difficulty",
  "estimated_minutes",
  "min_players",
  "max_players",
  "language",
  "original_work",
  "intro",
  "text",
  "public_goal",
  "start_card",
  "play_cards",
  "kind",
  "blocks",
  "type",
  "segments",
  "shareable",
  "asset_id",
  "caption",
  "role",
  "logic_notes",
  "supports_questions",
  "questions",
  "options",
  "correct_option_id",
  "explanation",
  "evidence_card_ids",
  "solution",
  "full_story",
  "timeline",
  "when",
  "event",
  "reveal_notes",
  "scoring",
  "correct_answer",
  "irrelevant_revealed",
  "minimum_discards",
  "missing_discard",
  "path",
  "mime",
  "alt",
]);
function safeImportIssues(value: unknown): ImportIssue[] {
  if (!Array.isArray(value)) return [];
  return value.slice(0, 20).flatMap((item) => {
    if (
      !item ||
      typeof item !== "object" ||
      typeof item.code !== "string" ||
      typeof item.path !== "string"
    )
      return [];
    const parts = item.path.split("/").slice(1);
    const path =
      item.path.startsWith("/") &&
      item.path.length <= 240 &&
      parts.every(
        (p: string) => safePathNames.has(p) || /^\d{1,4}$/.test(p) || p === "",
      )
        ? item.path
        : "/";
    return [
      {
        path,
        code: Object.hasOwn(importIssueLabels, item.code)
          ? item.code
          : "VALIDATION_ERROR",
      },
    ];
  });
}
