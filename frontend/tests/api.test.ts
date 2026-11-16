import test from "node:test";
import assert from "node:assert/strict";
import {
  ApiClient,
  ApiError,
  NetworkError,
  parseRoom,
  restorePending,
  validNickname,
} from "../src/api.ts";
import type { Pending } from "../src/contracts.ts";

const pending: Pending = {
  code: "RoomCode_123",
  memberId: "11111111-1111-4111-8111-111111111111",
  kind: "action",
  body: {
    client_action_id: "22222222-2222-4222-8222-222222222222",
    expected_revision: 2,
    type: "play_card",
    payload: { card_id: "clue-test", mode: "discard", confirmed: true },
  },
};

test("room links are converted to local room codes, never external API URLs", () => {
  assert.equal(
    parseRoom(" https://example.invalid/rooms/RoomCode_123 "),
    "RoomCode_123",
  );
  assert.equal(parseRoom("/rooms/RoomCode_123"), "RoomCode_123");
  for (const input of [
    "../etc",
    "abc",
    "https://host.invalid/no-room",
    "javascript:alert(1)",
  ])
    assert.equal(parseRoom(input), null);
});
test("nickname validation follows trimmed Unicode character limits", () => {
  assert.equal(validNickname("  Анна  "), true);
  assert.equal(validNickname("🕵".repeat(20)), true);
  for (const input of ["А", " ", "a".repeat(21), "a\nb", "a\u200bb"])
    assert.equal(validNickname(input), false);
});
test("valid pending command survives reload; invalid storage is not submitted", () => {
  assert.deepEqual(restorePending(JSON.stringify(pending)), pending);
  for (const value of [
    null,
    "{",
    "null",
    "[]",
    JSON.stringify({ ...pending, code: "../wrong" }),
    JSON.stringify({
      ...pending,
      body: { ...pending.body, expected_revision: -1 },
    }),
  ])
    assert.equal(restorePending(value), null);
});
test("a lost command response can only be retried with the original body and ID", async () => {
  const bodies: string[] = [];
  const headers: HeadersInit[] = [];
  let first = true;
  const api = new ApiClient(async (input, init) => {
    if (input === "/api/v1/sessions")
      return new Response(
        JSON.stringify({ csrf_token: "test-csrf", rooms: [] }),
      );
    bodies.push(String(init?.body));
    headers.push(init?.headers ?? {});
    assert.equal(init?.credentials, "same-origin");
    if (first) {
      first = false;
      throw new TypeError("network interrupted");
    }
    return new Response(
      JSON.stringify({
        revision: 3,
        phase: "PLAYING",
        client_action_id: pending.body.client_action_id,
      }),
    );
  });
  await api.bootstrap();
  await assert.rejects(() => api.execute(pending), NetworkError);
  await api.execute(pending);
  assert.equal(bodies.length, 2);
  assert.equal(bodies[0], bodies[1]);
  assert.equal(new Headers(headers[1]).get("X-CSRF-Token"), "test-csrf");
  assert.equal(JSON.stringify(pending).includes("csrf"), false);
});
test("errors never reflect a proxy body or private submitted data", async () => {
  const api = new ApiClient(
    async () => new Response("PRIVATE_SENTINEL", { status: 503 }),
  );
  try {
    await api.execute(pending);
    assert.fail("request should fail");
  } catch (error) {
    assert.equal(error instanceof ApiError, true);
    assert.equal(String(error).includes("PRIVATE_SENTINEL"), false);
  }
});

test("board intent restores exact object version and coordinates without credentials", () => {
  const board = {
    ...pending,
    kind: "action",
    body: {
      client_action_id: pending.body.client_action_id,
      expected_revision: 7,
      type: "board_mutation",
      payload: {
        op: "card_transform",
        card_id: "public-card",
        x: 123.5,
        y: -44,
        rotation: 90,
        expected_version: 2,
      },
    },
  };
  assert.deepEqual(restorePending(JSON.stringify(board)), board);
  const conflict = new ApiError("BOARD_VERSION_CONFLICT", 409);
  assert.match(conflict.message, /не сохранено/);
});

test("case upload uses room multipart and CSRF without manually setting boundary", async () => {
  const seen: { input: string; init?: RequestInit }[] = [];
  const api = new ApiClient(async (input, init) => {
    seen.push({ input: String(input), init });
    return new Response(
      JSON.stringify(
        input === "/api/v1/sessions"
          ? { csrf_token: "import-test-csrf", rooms: [] }
          : {
              case: { id: "test-case", meta: { title: "Public title" } },
              already_imported: false,
            },
      ),
    );
  });
  await api.bootstrap();
  const file = new File(["test bytes"], "test.detective", {
    type: "application/octet-stream",
  });
  const result = await api.importCase("RoomCode_123", file);
  const request = seen[1];
  assert.equal(request.input, "/api/v1/rooms/RoomCode_123/cases/import");
  assert.equal(request.init?.method, "POST");
  assert.equal(request.init?.credentials, "same-origin");
  const headers = new Headers(request.init?.headers);
  assert.equal(headers.get("X-CSRF-Token"), "import-test-csrf");
  assert.equal(headers.has("Content-Type"), false);
  assert.equal(request.init?.body instanceof FormData, true);
  const data = request.init?.body as FormData;
  assert.deepEqual([...data.keys()], ["file"]);
  assert.equal((data.get("file") as File).name, "test.detective");
  assert.equal(result.already_imported, false);
});

test("import diagnostics discard raw messages, unknown codes and secret path segments", async () => {
  const api = new ApiClient(
    async () =>
      new Response(
        JSON.stringify({
          error: {
            code: "CASE_VALIDATION_FAILED",
            message: "PRIVATE_SENTINEL",
            issues: [
              {
                path: "/play_cards/0/id",
                code: "DUPLICATE",
                message: "PRIVATE_SENTINEL",
              },
              { path: "/solution/PRIVATE_SENTINEL", code: "PRIVATE_SENTINEL" },
              {
                path: "<img src=x onerror=PRIVATE_SENTINEL>",
                code: "INVALID_ZIP",
              },
              ...Array.from({ length: 30 }, () => ({
                path: "/",
                code: "SCHEMA_INVALID",
              })),
            ],
          },
        }),
        { status: 422 },
      ),
  );
  try {
    await api.importCase("RoomCode_123", new File(["bad"], "bad.detective"));
    assert.fail("upload should fail");
  } catch (error) {
    assert.ok(error instanceof ApiError);
    assert.equal(error.issues.length, 20);
    assert.deepEqual(error.issues[0], {
      path: "/play_cards/0/id",
      code: "DUPLICATE",
    });
    assert.deepEqual(error.issues[1], { path: "/", code: "VALIDATION_ERROR" });
    assert.equal(
      JSON.stringify({ message: error.message, issues: error.issues }).includes(
        "PRIVATE_SENTINEL",
      ),
      false,
    );
  }
});

test("room catalog GET preserves cookie and does not expose another room in request", async () => {
  let path = "";
  const api = new ApiClient(async (input, init) => {
    path = String(input);
    assert.equal(init?.method, "GET");
    assert.equal(init?.credentials, "same-origin");
    return new Response("[]");
  });
  await api.roomCases("RoomCode_123");
  assert.equal(path, "/api/v1/rooms/RoomCode_123/cases");
});
