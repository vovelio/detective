import test from "node:test";
import assert from "node:assert/strict";
import { ApiError } from "../src/api.ts";
import {
  RoomSync,
  type SocketPort,
  type ConnectionStatus,
} from "../src/realtime.ts";
import type { RoomInfo, Snapshot } from "../src/contracts.ts";

class FakeSocket implements SocketPort {
  readyState = 1;
  onmessage: SocketPort["onmessage"] = null;
  onclose: SocketPort["onclose"] = null;
  onerror: SocketPort["onerror"] = null;
  sent: Record<string, unknown>[] = [];
  send(value: string) {
    this.sent.push(JSON.parse(value));
  }
  close() {
    this.readyState = 3;
  }
  message(value: Record<string, unknown>) {
    this.onmessage?.({
      data: JSON.stringify({ protocol_version: 1, ...value }),
    });
  }
  disconnected(code = 1006) {
    this.readyState = 3;
    this.onclose?.({ code });
  }
}
const room = (revision: number): RoomInfo => ({
  code: "RoomCode_123",
  member_id: "own",
  host_id: "own",
  revision,
  phase: "LOBBY",
  selected_case_id: null,
  players: [],
});
const settle = () => new Promise<void>((resolve) => setImmediate(resolve));
function harness(load: (signal: AbortSignal) => Promise<Snapshot>) {
  const sockets: FakeSocket[] = [],
    data: Snapshot[] = [],
    statuses: ConnectionStatus[] = [],
    errors: Error[] = [];
  const timers: { call: () => void; ms: number }[] = [];
  const sync = new RoomSync({
    url: "ws://local.invalid/ws/v1/rooms/RoomCode_123",
    load,
    data: (v) => data.push(v),
    status: (s) => statuses.push(s),
    presence: () => {},
    fatal: (e) => errors.push(e),
    socket: () => {
      const s = new FakeSocket();
      sockets.push(s);
      return s;
    },
    random: () => 0.5,
    delay: (call, ms) => {
      timers.push({ call, ms });
      return 0 as unknown as ReturnType<typeof setTimeout>;
    },
  });
  sync.start();
  return { sync, sockets, data, statuses, errors, timers };
}

test("hello loads personal snapshot and resumes only its applied revision", async () => {
  const h = harness(async () => room(3));
  h.sockets[0].message({ type: "hello", revision: 2 });
  await settle();
  assert.equal(h.data[0].revision, 3);
  assert.deepEqual(h.sockets[0].sent.at(-1), {
    type: "resume",
    protocol_version: 1,
    revision: 3,
  });
  h.sockets[0].message({ type: "synced", revision: 3 });
  assert.equal(h.statuses.at(-1), "connected");
  h.sockets[0].message({ type: "ping" });
  assert.equal(h.sockets[0].sent.at(-1)?.type, "pong");
  h.sync.stop();
});
test("old HTTP response cannot replace a new connection generation", async () => {
  const resolves: ((s: Snapshot) => void)[] = [];
  const h = harness(() => new Promise((resolve) => resolves.push(resolve)));
  h.sockets[0].message({ type: "hello", revision: 1 });
  h.sync.reconnect();
  h.sockets[1].message({ type: "hello", revision: 5 });
  resolves[1](room(5));
  await settle();
  resolves[0](room(1));
  await settle();
  assert.deepEqual(
    h.data.map((s) => s.revision),
    [5],
  );
  h.sync.stop();
});
test("notifications during fetch are coalesced and a snapshot gap is repaired", async () => {
  const resolves: ((s: Snapshot) => void)[] = [];
  const h = harness(() => new Promise((resolve) => resolves.push(resolve)));
  h.sockets[0].message({ type: "hello", revision: 1 });
  for (const revision of [2, 3, 4])
    h.sockets[0].message({ type: "state_changed", revision });
  assert.equal(resolves.length, 1);
  resolves[0](room(2));
  await settle();
  assert.equal(resolves.length, 2);
  resolves[1](room(4));
  await settle();
  assert.equal(h.data.at(-1)?.revision, 4);
  h.sockets[0].message({ type: "state_changed", revision: 2 });
  await settle();
  assert.equal(resolves.length, 2);
  h.sync.stop();
});
test("lobby notifications refresh even at the same game revision", async () => {
  let loads = 0;
  const h = harness(async () => {
    loads++;
    return room(0);
  });
  h.sockets[0].message({ type: "hello", revision: 0 });
  await settle();
  h.sockets[0].message({ type: "room_changed", revision: 0 });
  await settle();
  assert.equal(loads, 2);
  h.sync.stop();
});
test("backoff is bounded and reconnects do not send game commands", () => {
  const h = harness(async () => room(0));
  for (let i = 0; i < 12; i++) {
    h.sockets.at(-1)!.disconnected();
    h.timers.at(-1)!.call();
  }
  assert.ok(h.timers.every((t) => t.ms >= 500 && t.ms <= 10000));
  assert.equal(
    h.sockets.every((s) =>
      s.sent.every((m) => ["resume", "pong"].includes(String(m.type))),
    ),
    true,
  );
  h.sync.stop();
});
test("lost authentication clears the channel without automatic new identity", async () => {
  const h = harness(async () => {
    throw new ApiError("SESSION_REQUIRED", 401);
  });
  h.sockets[0].message({ type: "hello", revision: 0 });
  await settle();
  assert.equal(h.errors.length, 1);
  assert.equal(h.timers.length, 0);
  assert.equal(h.sockets[0].readyState, 3);
});

test("a silent handshake is retried even without a browser close event", async () => {
  const socket = new FakeSocket();
  const statuses: ConnectionStatus[] = [];
  const sync = new RoomSync({
    url: "ws://local.invalid/ws/v1/rooms/RoomCode_123",
    load: async () => room(0),
    data: () => {},
    presence: () => {},
    fatal: () => {},
    status: (value) => statuses.push(value),
    socket: () => socket,
    handshakeTimeoutMs: 5,
    delay: () => 0 as unknown as ReturnType<typeof setTimeout>,
  });
  sync.start();
  await new Promise((resolve) => setTimeout(resolve, 20));
  assert.equal(statuses.at(-1), "reconnecting");
  assert.equal(socket.readyState, 3);
  sync.stop();
});

test("cursor and public previews are ephemeral, bounded and rate limited", async () => {
  const socket = new FakeSocket();
  let now = 1000;
  const cursors: unknown[] = [],
    previews: unknown[] = [];
  const sync = new RoomSync({
    url: "ws://local.invalid/ws/v1/rooms/RoomCode_123",
    load: async () => room(2),
    data: () => {},
    status: () => {},
    presence: () => {},
    fatal: () => {},
    socket: () => socket,
    now: () => now,
    cursor: (id, x, y) => cursors.push({ id, x, y }),
    preview: (id, value) => previews.push({ id, ...value }),
  });
  sync.start();
  sync.cursor(1, 2);
  assert.equal(socket.sent.length, 0);
  socket.message({ type: "hello", revision: 2 });
  await settle();
  socket.message({ type: "synced", revision: 2 });
  const p = { card_id: "public-card", x: 30, y: 40, rotation: 15, version: 0 };
  sync.cursor(10, 20);
  sync.cursor(11, 21);
  sync.previewCard(p);
  sync.previewCard({ ...p, x: 31 });
  assert.equal(socket.sent.filter((m) => m.type === "cursor").length, 1);
  assert.equal(socket.sent.filter((m) => m.type === "card_preview").length, 1);
  now += 250;
  sync.cursor(Infinity, 0);
  sync.cursor(1000001, 0);
  sync.previewCard({ ...p, x: NaN });
  sync.cursor(-30, -40);
  sync.previewCard({ ...p, x: 32 });
  assert.equal(socket.sent.filter((m) => m.type === "cursor").length, 2);
  assert.deepEqual(socket.sent.at(-1), {
    type: "card_preview",
    protocol_version: 1,
    ...p,
    x: 32,
  });
  socket.message({ type: "cursor", member_id: "other", x: 12, y: 13 });
  socket.message({ type: "card_preview", member_id: "other", ...p });
  assert.deepEqual(cursors, [{ id: "other", x: 12, y: 13 }]);
  assert.deepEqual(previews, [{ id: "other", ...p }]);
  sync.stop();
  now += 1000;
  sync.cursor(1, 2);
  sync.previewCard(p);
  assert.equal(socket.sent.filter((m) => m.type === "card_preview").length, 2);
});

test("invalid preview frames stop the transport without echoing payload", () => {
  const h = harness(async () => room(1));
  h.sockets[0].message({
    type: "card_preview",
    member_id: "other",
    card_id: "hidden",
    x: 0,
    y: 0,
    rotation: 0,
    version: -1,
  });
  assert.equal(h.errors.length, 1);
  assert.equal(h.errors[0].message.includes("hidden"), false);
  assert.equal(h.sockets[0].readyState, 3);
});
