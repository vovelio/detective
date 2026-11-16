import { ApiError } from "./api.ts";
import type { CardPreview, Snapshot } from "./contracts.ts";

export type ConnectionStatus =
  | "connecting"
  | "connected"
  | "reconnecting"
  | "syncing"
  | "idle";
export interface SocketPort {
  readyState: number;
  onmessage: ((event: { data: unknown }) => void) | null;
  onclose: ((event: { code: number }) => void) | null;
  onerror: (() => void) | null;
  send(data: string): void;
  close(code?: number): void;
}
function browserSocket(url: string): SocketPort {
  const real = new WebSocket(url, "detective.v1");
  const port: SocketPort = {
    get readyState() {
      return real.readyState;
    },
    onmessage: null,
    onclose: null,
    onerror: null,
    send: (data) => real.send(data),
    close: (code) => real.close(code),
  };
  real.onmessage = (event) => port.onmessage?.({ data: event.data });
  real.onclose = (event) => port.onclose?.({ code: event.code });
  real.onerror = () => port.onerror?.();
  return port;
}
export interface SyncOptions {
  url: string;
  load: (signal: AbortSignal) => Promise<Snapshot>;
  data: (snapshot: Snapshot) => void;
  status: (status: ConnectionStatus) => void;
  presence: (players: Record<string, boolean>) => void;
  fatal: (error: Error) => void;
  cursor?: (memberId: string, x: number, y: number) => void;
  preview?: (memberId: string, preview: CardPreview) => void;
  now?: () => number;
  socket?: (url: string) => SocketPort;
  delay?: (callback: () => void, ms: number) => ReturnType<typeof setTimeout>;
  random?: () => number;
  handshakeTimeoutMs?: number;
  idleTimeoutMs?: number;
}
export class RoomSync {
  private socket: SocketPort | null = null;
  private generation = 0;
  private stopped = false;
  private attempt = 0;
  private timer: ReturnType<typeof setTimeout> | null = null;
  private watchdog: ReturnType<typeof setTimeout> | null = null;
  private fetching = false;
  private dirty = false;
  private target = -1;
  private applied = -1;
  private presenceSequence = -1;
  private abort: AbortController | null = null;
  private options: SyncOptions;
  private cursorAt = -Infinity;
  private previewAt = -Infinity;
  private ready = false;
  constructor(options: SyncOptions) {
    this.options = options;
  }
  start(): void {
    this.stopped = false;
    this.connect();
  }
  stop(): void {
    this.stopped = true;
    this.generation++;
    if (this.timer !== null) clearTimeout(this.timer);
    this.timer = null;
    this.abort?.abort();
    this.detach();
  }
  reconnect(): void {
    if (this.stopped) return;
    if (this.timer !== null) clearTimeout(this.timer);
    this.timer = null;
    this.detach();
    this.connect();
  }
  invalidate(): void {
    this.refresh(this.generation);
  }
  cursor(x: number, y: number): void {
    const now = (this.options.now ?? Date.now)();
    if (
      !this.ready ||
      !validCoordinate(x) ||
      !validCoordinate(y) ||
      now - this.cursorAt < 250
    )
      return;
    this.cursorAt = now;
    this.send({ type: "cursor", protocol_version: 1, x, y });
  }
  previewCard(value: CardPreview): void {
    const now = (this.options.now ?? Date.now)();
    if (!this.ready || !validPreview(value) || now - this.previewAt < 125)
      return;
    this.previewAt = now;
    this.send({
      type: "card_preview",
      protocol_version: 1,
      card_id: value.card_id,
      x: value.x,
      y: value.y,
      rotation: value.rotation,
      version: value.version,
    });
  }
  private detach(): void {
    this.ready = false;
    if (this.watchdog !== null) clearTimeout(this.watchdog);
    this.watchdog = null;
    if (this.socket) {
      this.socket.onmessage = null;
      this.socket.onclose = null;
      this.socket.onerror = null;
      this.socket.close(1000);
      this.socket = null;
    }
  }
  private connect(): void {
    if (this.stopped) return;
    const generation = ++this.generation;
    this.abort?.abort();
    this.fetching = false;
    this.dirty = false;
    this.applied = -1;
    this.target = -1;
    this.presenceSequence = -1;
    this.ready = false;
    this.cursorAt = this.previewAt = -Infinity;
    this.options.presence({});
    this.options.status(this.attempt ? "reconnecting" : "connecting");
    try {
      const socket = (this.options.socket ?? browserSocket)(this.options.url);
      this.socket = socket;
      this.watch(this.options.handshakeTimeoutMs ?? 15000);
      socket.onmessage = (event) => {
        if (generation !== this.generation || this.stopped) return;
        try {
          if (typeof event.data !== "string" || event.data.length > 8192)
            throw new Error();
          const msg = JSON.parse(event.data) as Record<string, unknown>;
          if (msg.protocol_version !== 1 || typeof msg.type !== "string")
            throw new Error();
          this.watch(this.options.idleTimeoutMs ?? 45000);
          if (msg.type === "ping") {
            this.send({ type: "pong", protocol_version: 1 });
            return;
          }
          if (msg.type === "cursor" || msg.type === "card_preview") {
            if (
              typeof msg.member_id !== "string" ||
              !validCoordinate(msg.x) ||
              !validCoordinate(msg.y)
            )
              throw new Error();
            if (msg.type === "cursor")
              this.options.cursor?.(msg.member_id, msg.x, msg.y);
            else {
              if (!validPreview(msg)) throw new Error();
              this.options.preview?.(msg.member_id, {
                card_id: msg.card_id,
                x: msg.x,
                y: msg.y,
                rotation: msg.rotation,
                version: msg.version,
              });
            }
            return;
          }
          if (msg.type === "presence") {
            if (!Number.isInteger(msg.sequence) || !Array.isArray(msg.players))
              throw new Error();
            if ((msg.sequence as number) <= this.presenceSequence) return;
            this.presenceSequence = msg.sequence as number;
            const players: Record<string, boolean> = {};
            for (const player of msg.players as {
              member_id: unknown;
              online: unknown;
            }[]) {
              if (
                typeof player.member_id !== "string" ||
                typeof player.online !== "boolean"
              )
                throw new Error();
              players[player.member_id] = player.online;
            }
            this.options.presence(players);
            return;
          }
          if (
            [
              "hello",
              "state_changed",
              "room_changed",
              "sync_required",
              "synced",
            ].includes(msg.type)
          ) {
            if (!Number.isInteger(msg.revision) || (msg.revision as number) < 0)
              throw new Error();
            const revision = msg.revision as number;
            if (msg.type === "synced") {
              if (
                this.applied === revision &&
                this.target <= revision &&
                !this.fetching &&
                !this.dirty
              ) {
                this.ready = true;
                this.attempt = 0;
                this.options.status("connected");
              }
              return;
            }
            if (msg.type === "sync_required" && revision < this.applied)
              this.applied = -1;
            this.target = Math.max(this.target, revision);
            if (msg.type !== "state_changed" || revision > this.applied)
              this.refresh(generation);
          }
        } catch {
          this.options.fatal(
            new Error("Сервер прислал неподдерживаемое сообщение."),
          );
          this.stop();
        }
      };
      socket.onclose = (event) => {
        if (generation !== this.generation || this.stopped) return;
        this.abort?.abort();
        this.options.presence({});
        if (event.code === 4401 || event.code === 4403) {
          this.options.fatal(new ApiError("SESSION_EXPIRED_OR_INVALID", 401));
          this.stop();
          return;
        }
        this.schedule();
      };
      socket.onerror = () => {
        /* close event drives one reconnect timer */
      };
    } catch {
      this.schedule();
    }
  }
  private watch(ms: number): void {
    if (this.watchdog !== null) clearTimeout(this.watchdog);
    this.watchdog = setTimeout(() => {
      if (this.stopped) return;
      this.abort?.abort();
      this.detach();
      this.schedule();
    }, ms);
  }
  private send(message: Record<string, unknown>): void {
    if (this.socket?.readyState === 1)
      this.socket.send(JSON.stringify(message));
  }
  private schedule(): void {
    if (this.stopped || this.timer !== null) return;
    this.ready = false;
    this.options.presence({});
    this.options.status("reconnecting");
    const ms = Math.min(
      10000,
      500 *
        2 ** Math.min(this.attempt++, 5) *
        (0.8 + (this.options.random ?? Math.random)() * 0.4),
    );
    this.timer = (this.options.delay ?? setTimeout)(() => {
      this.timer = null;
      this.detach();
      this.connect();
    }, ms);
  }
  private refresh(generation: number): void {
    if (this.stopped || this.socket?.readyState !== 1) return;
    this.dirty = true;
    if (this.fetching) return;
    this.fetching = true;
    this.options.status("syncing");
    void this.load(generation);
  }
  private async load(generation: number): Promise<void> {
    try {
      while (this.dirty && !this.stopped && generation === this.generation) {
        this.dirty = false;
        this.abort = new AbortController();
        const snapshot = await this.options.load(this.abort.signal);
        if (this.stopped || generation !== this.generation) return;
        if (snapshot.revision >= this.applied) {
          this.applied = snapshot.revision;
          this.options.data(snapshot);
        }
        this.send({
          type: "resume",
          protocol_version: 1,
          revision: this.applied,
        });
        if (this.target > this.applied) this.dirty = true;
      }
    } catch (error) {
      if (
        this.stopped ||
        generation !== this.generation ||
        (error instanceof DOMException && error.name === "AbortError")
      )
        return;
      if (error instanceof ApiError && [401, 403].includes(error.status)) {
        this.options.fatal(error);
        this.stop();
      } else {
        this.detach();
        this.schedule();
      }
    } finally {
      if (generation === this.generation) this.fetching = false;
    }
  }
}

function validCoordinate(value: unknown): value is number {
  return (
    typeof value === "number" &&
    Number.isFinite(value) &&
    Math.abs(value) <= 1000000
  );
}
function validPreview(value: unknown): value is CardPreview {
  if (!value || typeof value !== "object") return false;
  const p = value as Record<string, unknown>;
  return (
    typeof p.card_id === "string" &&
    p.card_id.length <= 96 &&
    validCoordinate(p.x) &&
    validCoordinate(p.y) &&
    typeof p.rotation === "number" &&
    Number.isFinite(p.rotation) &&
    Number.isInteger(p.version) &&
    (p.version as number) >= 0
  );
}
