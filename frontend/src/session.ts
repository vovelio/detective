import {
  ApiClient,
  ApiError,
  NetworkError,
  parseRoom,
  restorePending,
} from "./api.ts";
import type {
  ActionBody,
  CardPreview,
  ActionType,
  CaseInfo,
  ImportState,
  Payloads,
  Pending,
  RoomInfo,
  Snapshot,
} from "./contracts.ts";
import { RoomSync, type ConnectionStatus } from "./realtime.ts";
import { isGame } from "./contracts.ts";

const PENDING_KEY = "detective.pendingAction.v1";
export interface AppState {
  loading: boolean;
  rooms: RoomInfo[];
  cases: CaseInfo[];
  importing: ImportState;
  room: RoomInfo | null;
  snapshot: Snapshot | null;
  connection: ConnectionStatus;
  online: Record<string, boolean>;
  cursors: Record<string, { x: number; y: number; updatedAt: number }>;
  previews: Record<string, CardPreview & { updatedAt: number }>;
  busy: boolean;
  pending: boolean;
  error: string | null;
  sessionLost: boolean;
  invitation: string;
}
export class SessionController {
  private value: AppState = {
    loading: true,
    rooms: [],
    cases: [],
    importing: { status: "idle", message: "", issues: [] },
    room: null,
    snapshot: null,
    connection: "idle",
    online: {},
    cursors: {},
    previews: {},
    busy: false,
    pending: false,
    error: null,
    sessionLost: false,
    invitation: "",
  };
  private listeners = new Set<() => void>();
  private sync: RoomSync | null = null;
  private pending: Pending | null = null;
  private roomGeneration = 0;
  private catalogRequest = 0;
  private uploadAbort: AbortController | null = null;
  private online = () => this.sync?.reconnect();
  private offline = () =>
    this.update({
      connection: "reconnecting",
      online: {},
      cursors: {},
      previews: {},
    });
  private api: ApiClient;
  constructor(api = new ApiClient()) {
    this.api = api;
  }
  getSnapshot = (): AppState => this.value;
  subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener);
    return () => {
      this.listeners.delete(listener);
    };
  };
  private update(patch: Partial<AppState>): void {
    this.value = { ...this.value, ...patch };
    for (const listener of this.listeners) listener();
  }
  async bootstrap(): Promise<void> {
    this.update({ loading: true, error: null });
    try {
      const session = await this.api.bootstrap();
      const cases = await this.api.cases();
      const code = parseRoom(window.location.pathname);
      this.update({
        rooms: session.rooms,
        cases,
        sessionLost: false,
        invitation: code ?? "",
      });
      if (code && session.rooms.some((room) => room.code === code))
        await this.openRoom(code, false);
      else if (code)
        this.update({
          error:
            "Введите имя для первого входа. Если прежняя cookie удалена, автоматически вернуться на прежнее место невозможно.",
        });
    } catch (error) {
      this.handleError(error);
    } finally {
      this.update({ loading: false });
    }
  }
  private handleError(error: unknown): void {
    if (error instanceof ApiError && error.status === 401) {
      this.sync?.stop();
      this.api.clearCredentials();
      this.update({
        snapshot: null,
        room: null,
        rooms: [],
        online: {},
        cursors: {},
        previews: {},
        sessionLost: true,
        connection: "idle",
      });
    }
    this.update({
      error:
        error instanceof Error
          ? error.message
          : "Не удалось выполнить действие.",
    });
  }
  async create(nickname: string): Promise<void> {
    await this.operation(async () => {
      try {
        const room = await this.api.create(nickname);
        await this.openRoom(room.code);
      } catch (error) {
        if (error instanceof NetworkError)
          throw new Error(
            "Создание комнаты не подтверждено. Обновите список моих комнат перед повторной попыткой, чтобы не создать вторую.",
          );
        throw error;
      }
    });
  }
  async join(nickname: string, input: string): Promise<void> {
    const code = parseRoom(input);
    if (!code) {
      this.update({
        error: "Введите код комнаты или полную пригласительную ссылку.",
      });
      return;
    }
    await this.operation(async () => {
      await this.api.join(code, nickname);
      await this.openRoom(code);
    });
  }
  async openRoom(code: string, push = true): Promise<void> {
    const generation = ++this.roomGeneration;
    this.uploadAbort?.abort();
    this.uploadAbort = null;
    this.sync?.stop();
    this.sync = null;
    this.update({
      snapshot: null,
      room: null,
      cases: [],
      busy: false,
      importing: { status: "idle", message: "", issues: [] },
      connection: "connecting",
      error: null,
      online: {},
      cursors: {},
      previews: {},
    });
    try {
      const room = await this.api.room(code);
      if (generation !== this.roomGeneration) return;
      if (push) window.history.pushState({}, "", `/rooms/${code}`);
      this.pending = restorePending(
        sessionStorage.getItem(`${PENDING_KEY}:${code}`),
      );
      if (
        this.pending &&
        (this.pending.memberId !== room.member_id || this.pending.code !== code)
      )
        this.pending = null;
      this.update({ room, pending: this.pending !== null });
      this.sync = new RoomSync({
        url: `${location.protocol === "https:" ? "wss:" : "ws:"}//${location.host}/ws/v1/rooms/${code}`,
        load: async (signal) => {
          const snapshot = await this.api.snapshot(code, signal);
          if (generation !== this.roomGeneration || signal.aborted)
            return snapshot;
          if (isGame(snapshot)) {
            // Membership and catalog are fixed after start. The personal snapshot
            // already contains every changing public field; do not fetch the same
            // room and catalog for every board revision on each participant.
            this.update({
              room: {
                ...(this.value.room ?? room),
                phase: snapshot.phase,
                revision: snapshot.revision,
                host_id: snapshot.host_id,
                players: snapshot.players,
              },
            });
          } else {
            const request = ++this.catalogRequest;
            const [info, cases] = await Promise.all([
              this.api.room(code, signal),
              this.api.roomCases(code, signal),
            ]);
            if (generation === this.roomGeneration && !signal.aborted)
              this.update({
                room: info,
                ...(request === this.catalogRequest ? { cases } : {}),
              });
          }
          return snapshot;
        },
        data: (snapshot) => {
          if (generation === this.roomGeneration) this.update({ snapshot });
        },
        status: (connection) => {
          if (generation === this.roomGeneration) this.update({ connection });
        },
        presence: (online) => {
          if (generation === this.roomGeneration)
            this.update({
              online,
              cursors: Object.fromEntries(
                Object.entries(this.value.cursors).filter(([id]) => online[id]),
              ),
              previews: Object.fromEntries(
                Object.entries(this.value.previews).filter(
                  ([id]) => online[id],
                ),
              ),
            });
        },
        cursor: (memberId, x, y) => {
          if (
            generation === this.roomGeneration &&
            this.value.room?.players.some((p) => p.id === memberId) &&
            memberId !== this.value.room.member_id
          )
            this.update({
              cursors: {
                ...this.value.cursors,
                [memberId]: { x, y, updatedAt: Date.now() },
              },
            });
        },
        preview: (memberId, preview) => {
          const snapshot = this.value.snapshot;
          if (
            generation === this.roomGeneration &&
            snapshot &&
            "published" in snapshot &&
            snapshot.published.some((p) => p.card.id === preview.card_id) &&
            this.value.room?.players.some((p) => p.id === memberId) &&
            memberId !== this.value.room.member_id
          )
            this.update({
              previews: {
                ...this.value.previews,
                [memberId]: { ...preview, updatedAt: Date.now() },
              },
            });
        },
        fatal: (error) => {
          if (generation === this.roomGeneration) {
            this.update({
              snapshot: null,
              connection: "idle",
              online: {},
              cursors: {},
              previews: {},
            });
            this.handleError(error);
          }
        },
      });
      window.addEventListener("online", this.online);
      window.addEventListener("offline", this.offline);
      this.sync.start();
    } catch (error) {
      if (generation === this.roomGeneration) this.handleError(error);
    }
  }
  navigate(): void {
    this.roomGeneration++;
    this.uploadAbort?.abort();
    this.uploadAbort = null;
    this.sync?.stop();
    this.sync = null;
    window.removeEventListener("online", this.online);
    window.removeEventListener("offline", this.offline);
    this.update({
      room: null,
      snapshot: null,
      connection: "idle",
      online: {},
      cursors: {},
      previews: {},
      pending: false,
      busy: false,
      importing: { status: "idle", message: "", issues: [] },
    });
    void this.bootstrap();
  }
  home(): void {
    this.roomGeneration++;
    this.uploadAbort?.abort();
    this.uploadAbort = null;
    this.sync?.stop();
    this.sync = null;
    window.removeEventListener("online", this.online);
    window.removeEventListener("offline", this.offline);
    history.pushState({}, "", "/");
    this.update({
      room: null,
      snapshot: null,
      connection: "idle",
      online: {},
      cursors: {},
      previews: {},
      error: null,
      pending: false,
      busy: false,
      importing: { status: "idle", message: "", issues: [] },
    });
    void this.bootstrap();
  }
  dismissError(): void {
    this.update({ error: null });
  }
  cursor(x: number, y: number): void {
    this.sync?.cursor(x, y);
  }
  previewCard(value: CardPreview): void {
    this.sync?.previewCard(value);
  }
  reconnect(): void {
    this.sync?.reconnect();
  }
  async refreshCases(): Promise<void> {
    const room = this.value.room;
    if (!room || this.value.busy) return;
    const generation = this.roomGeneration;
    this.update({ busy: true, error: null });
    try {
      const request = ++this.catalogRequest;
      const cases = await this.api.roomCases(room.code);
      if (generation === this.roomGeneration && request === this.catalogRequest)
        this.update({ cases });
    } catch (error) {
      if (generation === this.roomGeneration) this.handleError(error);
    } finally {
      if (generation === this.roomGeneration) this.update({ busy: false });
    }
  }
  resetImport(): void {
    if (this.value.importing.status !== "uploading")
      this.update({ importing: { status: "idle", message: "", issues: [] } });
  }
  async importCase(file: File): Promise<void> {
    const room = this.value.room;
    if (
      !room ||
      room.member_id !== room.host_id ||
      room.phase !== "LOBBY" ||
      this.value.busy ||
      this.value.pending
    )
      return;
    const generation = this.roomGeneration;
    const abort = new AbortController();
    this.uploadAbort = abort;
    this.update({
      busy: true,
      error: null,
      importing: {
        status: "uploading",
        message: "Загрузка и проверка…",
        issues: [],
      },
    });
    try {
      const receipt = await this.api.importCase(room.code, file, abort.signal);
      if (generation !== this.roomGeneration || abort.signal.aborted) return;
      const request = ++this.catalogRequest;
      const cases = await this.api.roomCases(room.code, abort.signal);
      if (generation !== this.roomGeneration || abort.signal.aborted) return;
      this.update({
        ...(request === this.catalogRequest ? { cases } : {}),
        importing: {
          status: "success",
          message: receipt.already_imported
            ? "Это дело уже добавлено в комнату. Выберите его в списке."
            : "Дело добавлено. Выберите его в списке, чтобы начать расследование.",
          issues: [],
        },
      });
      this.sync?.invalidate();
    } catch (error) {
      if (generation !== this.roomGeneration || abort.signal.aborted) return;
      if (error instanceof ApiError && error.status === 401)
        this.handleError(error);
      else {
        const uncertain =
          error instanceof NetworkError ||
          (error instanceof ApiError && error.status >= 500);
        this.update({
          importing: {
            status: uncertain ? "uncertain" : "error",
            message: uncertain
              ? "Ответ не получен или сервер временно недоступен. Дело могло сохраниться: обновите список перед повторной загрузкой. Повтор того же файла безопасен."
              : error instanceof Error
                ? error.message
                : "Не удалось добавить дело.",
            issues: error instanceof ApiError ? error.issues : [],
          },
        });
      }
    } finally {
      if (generation === this.roomGeneration) {
        this.uploadAbort = null;
        this.update({ busy: false });
      }
    }
  }
  async selectCase(caseId: string): Promise<void> {
    const code = this.value.room?.code;
    if (code)
      await this.operation(async () => {
        await this.api.select(code, caseId);
        this.sync?.invalidate();
      });
  }
  async startGame(): Promise<void> {
    await this.sendIntent("start");
  }
  async command<K extends ActionType>(
    type: K,
    payload: Payloads[K],
  ): Promise<void> {
    await this.sendIntent("action", { type, payload } as Pick<
      ActionBody,
      "type" | "payload"
    >);
  }
  private async sendIntent(
    kind: Pending["kind"],
    action?: Pick<ActionBody, "type" | "payload">,
  ): Promise<void> {
    const { room, snapshot } = this.value;
    if (
      !room ||
      !snapshot ||
      this.value.busy ||
      this.value.pending ||
      !["connected", "syncing"].includes(this.value.connection)
    )
      return;
    const identity = {
      client_action_id: crypto.randomUUID(),
      expected_revision: snapshot.revision,
    };
    this.pending =
      kind === "start"
        ? { code: room.code, memberId: room.member_id, kind, body: identity }
        : {
            code: room.code,
            memberId: room.member_id,
            kind,
            body: { ...identity, ...action! } as ActionBody,
          };
    try {
      sessionStorage.setItem(
        `${PENDING_KEY}:${room.code}`,
        JSON.stringify(this.pending),
      );
    } catch {
      this.pending = null;
      this.update({
        error:
          "Браузер запретил сохранение идентификатора запроса. Действие не отправлено.",
      });
      return;
    }
    this.update({ pending: true });
    await this.retryPending();
  }
  async retryPending(): Promise<void> {
    const pending = this.pending;
    if (
      !pending ||
      this.value.busy ||
      pending.memberId !== this.value.room?.member_id
    )
      return;
    await this.operation(async () => {
      try {
        await this.api.execute(pending);
        this.clearPending();
        this.sync?.invalidate();
      } catch (error) {
        if (
          error instanceof ApiError &&
          error.status < 500 &&
          error.status !== 401 &&
          error.status !== 403
        )
          this.clearPending();
        if (error instanceof ApiError) this.sync?.invalidate();
        throw error;
      }
    });
  }
  private clearPending(): void {
    const code = this.pending?.code;
    this.pending = null;
    if (code) sessionStorage.removeItem(`${PENDING_KEY}:${code}`);
    this.update({ pending: false });
  }
  private async operation(operation: () => Promise<void>): Promise<void> {
    if (this.value.busy) return;
    this.update({ busy: true, error: null });
    try {
      await operation();
    } catch (error) {
      this.handleError(error);
    } finally {
      this.update({ busy: false });
    }
  }
}
