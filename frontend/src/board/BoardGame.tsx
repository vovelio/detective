import { createPortal } from "react-dom";
import {
  useEffect,
  useRef,
  useState,
  type PointerEvent,
  type ReactNode,
} from "react";
import type {
  GameSnapshot,
  CardData,
  BoardMutation,
  BoardObject,
} from "../contracts";
import type { AppState, SessionController } from "../session";
import type { Confirmation } from "../components/Dialog";
import { Card } from "../components/Card";
import { Players } from "../components/Players";
import {
  CARD_HEIGHT,
  CARD_WIDTH,
  screenToWorld,
  gridStyle,
  zoomAt,
  type Point,
  type Camera,
  type CardTransform,
} from "./geometry";

type Tool = "select" | "pan" | "pencil" | "eraser" | "arrow" | "note";
type Drag =
  | { kind: "hand"; card: CardData; screen: Point }
  | { kind: "pan"; start: Point; camera: Camera }
  | {
      kind: "card";
      id: string;
      version: number;
      start: Point;
      transform: CardTransform;
      current: CardTransform;
    }
  | {
      kind: "rotate";
      id: string;
      version: number;
      transform: CardTransform;
      current: CardTransform;
    }
  | {
      kind: "annotation";
      object: BoardObject;
      start: Point;
      current: Point;
      endpoint?: "from" | "to";
    }
  | { kind: "stroke"; points: Point[] }
  | { kind: "arrow"; from: Point; to: Point };
type Position = CardTransform & { version: number };
const editablePhases = ["INTRO", "PLAYING", "DISCUSSION"];

export function BoardGame({
  game,
  state,
  controller,
  confirm,
  phasePanel,
}: {
  game: GameSnapshot;
  state: AppState;
  controller: SessionController;
  confirm: (v: Confirmation) => void;
  phasePanel: ReactNode;
}) {
  const room = state.room!;
  const surface = useRef<HTMLDivElement>(null);
  const discard = useRef<HTMLElement>(null);
  const [camera, setCamera] = useState<Camera>({ x: 300, y: 150, zoom: 1 });
  const [tool, setTool] = useState<Tool>("select");
  const [drag, setDrag] = useState<Drag | null>(null);
  const [selected, setSelected] = useState<string | null>(null);

  const [menu, setMenu] = useState(false);
  const [information, setInformation] = useState<
    "description" | "questions" | "finish" | null
  >(null);
  const [reading, setReading] = useState<CardData | null>(null);
  const [noteFailure, setNoteFailure] = useState(false);
  const [note, setNote] = useState<{
    x: number;
    y: number;
    text: string;
    object?: BoardObject;
  } | null>(null);
  useEffect(() => {
    if (!note) setNoteFailure(false);
  }, [note]);
  const [confirmPublish, setConfirmPublish] = useState(() => {
    try {
      return localStorage.getItem("detective.confirmPublish") !== "false";
    } catch {
      return true;
    }
  });
  useEffect(() => {
    if (
      reading &&
      !game.private.hand.some((c) => c.id === reading.id) &&
      !game.published.some((p) => p.card.id === reading.id)
    )
      setReading(null);
  }, [game.private.hand, game.published, reading]);
  const locked =
    state.busy ||
    state.pending ||
    !["connected", "syncing"].includes(state.connection);
  const editable = editablePhases.includes(game.phase) && !locked;
  const myTurn =
    game.phase === "PLAYING" &&
    game.active_player_id === room.member_id &&
    !locked;
  const final = !editablePhases.includes(game.phase);
  const assets = [...game.assets, ...game.private.assets];
  const objects = game.board?.objects ?? [];
  const positions: Record<string, Position> = {};
  for (const item of game.published)
    positions[item.card.id] = { ...item.position, rotation: 0, version: 0 };
  for (const object of objects)
    if (object.kind === "card_position") {
      const p = object.payload as {
        card_id: string;
        x: number;
        y: number;
        rotation: number;
      };
      if (positions[p.card_id])
        positions[p.card_id] = {
          x: p.x,
          y: p.y,
          rotation: p.rotation,
          version: object.version,
        };
    }
  for (const [id, p] of Object.entries(state.previews))
    if (
      id !== room.member_id &&
      Date.now() - p.updatedAt < 2000 &&
      positions[p.card_id] &&
      positions[p.card_id].version === p.version
    )
      positions[p.card_id] = {
        x: p.x,
        y: p.y,
        rotation: p.rotation,
        version: p.version,
      };
  if (drag?.kind === "card" || drag?.kind === "rotate")
    positions[drag.id] = { ...drag.current, version: drag.version };
  useEffect(() => {
    setDrag(null);
    setNote(null);
    setInformation(null);
    setMenu(false);
  }, [room.code, game.phase, state.sessionLost]);
  useEffect(() => {
    if (locked) setDrag(null);
  }, [locked]);
  const [, tick] = useState(0);
  useEffect(() => {
    const timer = window.setInterval(() => tick((v) => v + 1), 250);
    return () => window.clearInterval(timer);
  }, []);
  useEffect(() => {
    if (
      drag?.kind === "hand" &&
      (!myTurn || !game.private.hand.some((c) => c.id === drag.card.id))
    )
      setDrag(null);
  }, [myTurn, game.private.hand, drag]);
  useEffect(() => {
    const element = surface.current;
    if (!element) return;
    const wheel = (event: WheelEvent) => {
      event.preventDefault();
      const rect = element.getBoundingClientRect();
      setCamera((c) =>
        zoomAt(
          c,
          { x: event.clientX - rect.left, y: event.clientY - rect.top },
          Math.exp(-event.deltaY * 0.001),
        ),
      );
    };
    element.addEventListener("wheel", wheel, { passive: false });
    return () => element.removeEventListener("wheel", wheel);
  }, []);
  useEffect(() => {
    const escape = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        setDrag(null);
        setNote(null);
        setReading(null);
        setInformation(null);
        setMenu(false);
      }
    };
    window.addEventListener("keydown", escape);
    return () => window.removeEventListener("keydown", escape);
  }, []);
  const local = (point: Point) => {
    const bounds = surface.current!.getBoundingClientRect();
    return { x: point.x - bounds.left, y: point.y - bounds.top };
  };
  const world = (point: Point) => screenToWorld(local(point), camera);
  const point = (event: { clientX: number; clientY: number }) => ({
    x: event.clientX,
    y: event.clientY,
  });
  const mutate = (payload: BoardMutation) => {
    void controller.command("board_mutation", payload);
  };
  function publish(
    card: CardData,
    position: Point,
    mode: "reveal" | "discard",
  ) {
    const run = () =>
      controller.command("play_card", {
        card_id: card.id,
        mode,
        confirmed: true,
        ...(mode === "reveal" ? position : {}),
      });
    if (mode === "reveal" && !confirmPublish) {
      void run();
      return;
    }
    confirm({
      title: mode === "reveal" ? "Опубликовать карту?" : "Сбросить карту?",
      text:
        mode === "reveal"
          ? "Карта станет видна всем в выбранном месте. После подтверждения ход перейдёт автоматически."
          : "Карта уйдёт в закрытый сброс и станет недоступна даже вам. Ход перейдёт автоматически.",
      confirm:
        mode === "reveal" ? "Подтвердить публикацию" : "Подтвердить сброс",
      run,
    });
  }
  function capture(event: PointerEvent) {
    event.preventDefault();
    event.currentTarget.setPointerCapture(event.pointerId);
  }
  function cardDown(event: PointerEvent, id: string, rotation = false) {
    if (!editable || event.button !== 0) return;
    if (tool === "pan" || tool === "arrow") return;
    event.stopPropagation();
    if (tool !== "select") return;
    capture(event);
    setSelected(id);
    const p = positions[id];
    setDrag(
      rotation
        ? { kind: "rotate", id, version: p.version, transform: p, current: p }
        : {
            kind: "card",
            id,
            version: p.version,
            start: world(point(event)),
            transform: p,
            current: p,
          },
    );
  }
  function annotationDown(
    event: PointerEvent,
    object: BoardObject,
    endpoint?: "from" | "to",
  ) {
    if (tool === "pan" || tool === "arrow") return;
    event.stopPropagation();
    if (!editable || event.button !== 0) return;
    if (tool === "eraser") {
      erase(object);
      return;
    }
    if (tool !== "select") return;
    capture(event);
    setSelected(object.id);
    const start = world(point(event));
    setDrag({ kind: "annotation", object, start, current: start, endpoint });
  }
  function annotationPayload(
    active: Extract<Drag, { kind: "annotation" }>,
    end: Point,
  ) {
    const dx = end.x - active.start.x,
      dy = end.y - active.start.y;
    if (active.object.kind === "note") {
      const p = active.object.payload;
      return { ...p, x: p.x + dx, y: p.y + dy };
    }
    const p = active.object.payload as { from: Point; to: Point };
    const shift = (key: "from" | "to") =>
      active.endpoint && active.endpoint !== key
        ? p[key]
        : { x: p[key].x + dx, y: p[key].y + dy };
    return { from: shift("from"), to: shift("to") };
  }
  function backgroundDown(event: PointerEvent<HTMLDivElement>) {
    if (event.button !== 0) return;
    if (
      tool !== "pan" &&
      tool !== "arrow" &&
      (event.target as HTMLElement).closest("[data-board-object]")
    )
      return;
    const p = world(point(event));
    if (tool === "note" && editable) {
      setNote({ ...p, text: "" });
      return;
    }
    if (tool === "eraser") return;
    capture(event);
    if (tool === "arrow" && editable)
      setDrag({ kind: "arrow", from: p, to: p });
    else if (tool === "pencil" && editable)
      setDrag({ kind: "stroke", points: [p] });
    else {
      setSelected(null);
      setDrag({ kind: "pan", start: point(event), camera });
    }
  }
  function move(event: PointerEvent) {
    const screen = point(event);
    if ((event.target as HTMLElement).closest("[data-testid=board-viewport]")) {
      const p = world(screen);
      controller.cursor(p.x, p.y);
    }
    if (!drag) return;
    if (drag.kind === "annotation")
      setDrag({ ...drag, current: world(screen) });
    if (drag.kind === "hand") setDrag({ ...drag, screen });
    if (drag.kind === "pan")
      setCamera({
        ...drag.camera,
        x: drag.camera.x + screen.x - drag.start.x,
        y: drag.camera.y + screen.y - drag.start.y,
      });
    if (drag.kind === "card") {
      const p = world(screen);
      const current = {
        ...drag.transform,
        x: drag.transform.x + p.x - drag.start.x,
        y: drag.transform.y + p.y - drag.start.y,
      };
      setDrag({ ...drag, current });
      controller.previewCard({
        card_id: drag.id,
        version: drag.version,
        ...current,
      });
    }
    if (drag.kind === "rotate") {
      const p = world(screen);
      const center = {
        x: drag.transform.x + CARD_WIDTH / 2,
        y: drag.transform.y + CARD_HEIGHT / 2,
      };
      const current = {
        ...drag.transform,
        rotation: Math.round(
          (Math.atan2(p.y - center.y, p.x - center.x) * 180) / Math.PI + 90,
        ),
      };
      setDrag({ ...drag, current });
      controller.previewCard({
        card_id: drag.id,
        version: drag.version,
        ...current,
      });
    }
    if (drag.kind === "arrow") setDrag({ ...drag, to: world(screen) });
    if (drag.kind === "stroke") {
      const p = world(screen);
      const last = drag.points.at(-1)!;
      if (
        Math.hypot(p.x - last.x, p.y - last.y) > 2 &&
        drag.points.length < 256
      )
        setDrag({ ...drag, points: [...drag.points, p] });
    }
  }
  function release(event: PointerEvent) {
    const active = drag;
    setDrag(null);
    if (!active) return;
    if (active.kind === "annotation" && editable) {
      const end = world(point(event));
      if (
        Math.hypot(end.x - active.start.x, end.y - active.start.y) *
          camera.zoom >
        4
      ) {
        mutate({
          op: active.object.kind === "note" ? "note_update" : "arrow_update",
          object_id: active.object.id,
          expected_version: active.object.version,
          ...annotationPayload(active, end),
        } as BoardMutation);
      } else if (active.object.kind === "note") {
        setNote({ ...active.object.payload, object: active.object });
      }
    }
    if (active.kind === "hand" && myTurn) {
      const screen = point(event),
        d = discard.current!.getBoundingClientRect(),
        b = surface.current!.getBoundingClientRect();
      if (
        screen.x >= d.left &&
        screen.x <= d.right &&
        screen.y >= d.top &&
        screen.y <= d.bottom
      )
        publish(active.card, { x: 0, y: 0 }, "discard");
      else if (
        screen.x >= b.left &&
        screen.x <= b.right &&
        screen.y >= b.top &&
        screen.y <= b.bottom &&
        !document
          .elementFromPoint(screen.x, screen.y)
          ?.closest(".board-hud,.app-header,.notices")
      )
        publish(active.card, world(screen), "reveal");
    }
    if ((active.kind === "card" || active.kind === "rotate") && editable) {
      const end = world(point(event));
      const current =
        active.kind === "card"
          ? {
              ...active.transform,
              x: active.transform.x + end.x - active.start.x,
              y: active.transform.y + end.y - active.start.y,
            }
          : {
              ...active.transform,
              rotation: Math.round(
                (Math.atan2(
                  end.y - active.transform.y - CARD_HEIGHT / 2,
                  end.x - active.transform.x - CARD_WIDTH / 2,
                ) *
                  180) /
                  Math.PI +
                  90,
              ),
            };
      if (
        current.x !== active.transform.x ||
        current.y !== active.transform.y ||
        current.rotation !== active.transform.rotation
      )
        mutate({
          op: "card_transform",
          card_id: active.id,
          expected_version: active.version,
          x: current.x,
          y: current.y,
          rotation: current.rotation,
        });
    }
    if (
      active.kind === "arrow" &&
      editable &&
      Math.hypot(
        active.from.x - world(point(event)).x,
        active.from.y - world(point(event)).y,
      ) > 5
    )
      mutate({
        op: "arrow_create",
        object_id: crypto.randomUUID(),
        from: active.from,
        to: world(point(event)),
      });
    if (active.kind === "stroke" && active.points.length > 1 && editable)
      mutate({
        op: "stroke_create",
        object_id: crypto.randomUUID(),
        points: active.points,
      });
  }
  function erase(object: BoardObject) {
    if (tool === "eraser" && editable)
      mutate({
        op: "object_delete",
        object_id: object.id,
        expected_version: object.version,
      });
  }
  const center = () =>
    setCamera({
      x: Math.max(
        180,
        (surface.current?.clientWidth ?? 1000) / 2 - CARD_WIDTH / 2,
      ),
      y: 130,
      zoom: 1,
    });
  return (
    <main
      className="board-game"
      data-phase={game.phase}
      data-revision={game.revision}
      onPointerMove={move}
      onPointerUp={release}
      onPointerCancel={() => setDrag(null)}
    >
      <div
        ref={surface}
        className="board-viewport"
        style={gridStyle(camera)}
        data-testid="board-viewport"
        aria-label="Общий стол"
        onPointerDown={backgroundDown}
      >
        <div
          className="board-world"
          data-testid="board-world"
          style={{
            transform: `translate(${camera.x}px,${camera.y}px) scale(${camera.zoom})`,
          }}
        >
          <svg
            className="board-drawings"
            width="1"
            height="1"
            overflow="visible"
            aria-label="Общие рисунки и связи"
          >
            <defs>
              <marker
                id="arrowhead"
                markerWidth="8"
                markerHeight="8"
                refX="7"
                refY="4"
                orient="auto"
              >
                <path d="M0,0 L8,4 L0,8 z" fill="currentColor" />
              </marker>
            </defs>
            {objects
              .filter((o) => o.kind === "stroke")
              .map((o) => {
                const p = o.payload as { points: Point[] };
                return (
                  <polyline
                    key={o.id}
                    data-board-object
                    data-testid="board-stroke"
                    data-object-id={o.id}
                    points={p.points.map((p) => `${p.x},${p.y}`).join(" ")}
                    fill="none"
                    stroke="currentColor"
                    strokeWidth="3"
                    strokeLinecap="round"
                    onPointerDown={(e) => {
                      if (tool !== "pan" && tool !== "arrow") {
                        e.stopPropagation();
                        erase(o);
                      }
                    }}
                  />
                );
              })}
            {objects
              .filter((o) => o.kind === "arrow")
              .map((o) => {
                const p = (
                  drag?.kind === "annotation" && drag.object.id === o.id
                    ? annotationPayload(drag, drag.current)
                    : o.payload
                ) as { from: Point; to: Point };
                return (
                  <g
                    key={o.id}
                    data-board-object
                    data-testid="board-arrow"
                    data-object-id={o.id}
                  >
                    <line
                      x1={p.from.x}
                      y1={p.from.y}
                      x2={p.to.x}
                      y2={p.to.y}
                      stroke="currentColor"
                      strokeWidth="3"
                      markerEnd="url(#arrowhead)"
                      pointerEvents="none"
                    />
                    <line
                      x1={p.from.x}
                      y1={p.from.y}
                      x2={p.to.x}
                      y2={p.to.y}
                      stroke="transparent"
                      strokeWidth={24 / camera.zoom}
                      strokeLinecap="round"
                      onPointerDown={(e) => annotationDown(e, o)}
                    />
                    {(["from", "to"] as const).map((key) => (
                      <circle
                        key={key}
                        data-testid={`arrow-${key}`}
                        cx={p[key].x}
                        cy={p[key].y}
                        r={12 / camera.zoom}
                        fill={
                          selected === o.id && tool === "select"
                            ? "#fff3b9"
                            : "transparent"
                        }
                        stroke={
                          selected === o.id && tool === "select"
                            ? "currentColor"
                            : "none"
                        }
                        style={{ pointerEvents: "all", cursor: "move" }}
                        onPointerDown={(e) => annotationDown(e, o, key)}
                      />
                    ))}
                  </g>
                );
              })}
            {drag?.kind === "arrow" && (
              <line
                x1={drag.from.x}
                y1={drag.from.y}
                x2={drag.to.x}
                y2={drag.to.y}
                stroke="currentColor"
                strokeWidth="3"
                markerEnd="url(#arrowhead)"
              />
            )}

            {drag?.kind === "stroke" && (
              <polyline
                points={drag.points.map((p) => `${p.x},${p.y}`).join(" ")}
                fill="none"
                stroke="currentColor"
                strokeWidth="3"
              />
            )}
          </svg>
          {game.published.map(({ card }) => {
            const p = positions[card.id];
            return (
              <div
                key={card.id}
                className={`board-card ${selected === card.id ? "selected" : ""}`}
                data-testid="board-card"
                data-board-object
                data-card-id={card.id}
                style={{
                  left: p.x,
                  top: p.y,
                  width: CARD_WIDTH,
                  height: CARD_HEIGHT,
                  transform: `rotate(${p.rotation}deg)`,
                }}
                onPointerDown={(event) => cardDown(event, card.id)}
              >
                <Card card={card} code={room.code} assets={assets} />
                <button
                  className="card-read"
                  aria-label={`Рассмотреть ${card.title}`}
                  onPointerDown={(e) => {
                    if (tool !== "pan") e.stopPropagation();
                  }}
                  onClick={() => {
                    if (tool !== "pan") setReading(card);
                  }}
                >
                  Читать
                </button>
                {editable && selected === card.id && tool === "select" && (
                  <button
                    className="rotation-handle"
                    aria-label="Повернуть карту"
                    data-testid="rotation-handle"
                    onPointerDown={(e) => cardDown(e, card.id, true)}
                  >
                    ↻
                  </button>
                )}
              </div>
            );
          })}
          {objects
            .filter((o) => o.kind === "note")
            .map((o) => {
              const p = (
                drag?.kind === "annotation" && drag.object.id === o.id
                  ? annotationPayload(drag, drag.current)
                  : o.payload
              ) as { x: number; y: number; text: string };
              return (
                <button
                  key={o.id}
                  className="board-note"
                  data-board-object
                  data-testid="board-note"
                  data-object-id={o.id}
                  style={{ left: p.x, top: p.y }}
                  onPointerDown={(e) => annotationDown(e, o)}
                  onClick={(e) => {
                    if (e.detail === 0 && editable && tool === "select")
                      setNote({ ...p, object: o });
                  }}
                >
                  {p.text}
                </button>
              );
            })}
          {Object.entries(state.cursors)
            .filter(
              ([id, c]) =>
                id !== room.member_id && Date.now() - c.updatedAt < 15000,
            )
            .map(([id, c]) => (
              <div
                className="board-cursor"
                data-testid="cursor"
                data-member-id={id}
                key={id}
                style={{ left: c.x, top: c.y }}
              >
                <span>➤</span>
                <small>
                  {game.players.find((p) => p.id === id)?.nickname ?? "Игрок"}
                </small>
              </div>
            ))}
        </div>
      </div>
      <div className="board-hud board-players">
        <Players
          players={game.players}
          hostId={game.host_id}
          ownId={room.member_id}
          activeId={game.active_player_id}
          online={state.online}
        />
      </div>
      <nav
        className="board-hud board-toolbar"
        data-testid="board-toolbar"
        aria-label="Инструменты доски"
      >
        {(
          [
            ["select", "↖", "Выбор"],
            ["pan", "✋", "Перемещение доски"],
            ["pencil", "✎", "Карандаш"],
            ["eraser", "⌫", "Ластик"],
            ["arrow", "↗", "Стрелка"],
            ["note", "▣", "Заметка"],
          ] as const
        ).map(([value, icon, label]) => (
          <button
            key={value}
            title={label}
            aria-label={label}
            aria-pressed={tool === value}
            disabled={!editable && value !== "pan"}
            onClick={() => {
              setTool(value);
            }}
          >
            {icon}
          </button>
        ))}
        <button onClick={center}>К центру</button>
        <button
          aria-label="Отдалить"
          onClick={() =>
            setCamera((c) =>
              zoomAt(
                c,
                {
                  x: (surface.current?.clientWidth ?? 1000) / 2,
                  y: (surface.current?.clientHeight ?? 700) / 2,
                },
                0.8,
              ),
            )
          }
        >
          −
        </button>
        <span>{Math.round(camera.zoom * 100)}%</span>
        <button
          aria-label="Приблизить"
          onClick={() =>
            setCamera((c) =>
              zoomAt(
                c,
                {
                  x: (surface.current?.clientWidth ?? 1000) / 2,
                  y: (surface.current?.clientHeight ?? 700) / 2,
                },
                1.25,
              ),
            )
          }
        >
          +
        </button>
      </nav>
      {createPortal(
        <div className="board-hud board-settings">
          <button
            data-testid="settings"
            aria-label="Настройки и материалы дела"
            aria-expanded={menu}
            onClick={() => setMenu(!menu)}
          >
            ⚙
          </button>
          {menu && (
            <section className="settings-menu">
              <h2>{game.case.meta.title}</h2>
              <button
                onClick={() => {
                  setInformation("description");
                  setMenu(false);
                }}
              >
                Описание дела
              </button>
              <button
                onClick={() => {
                  setInformation("questions");
                  setMenu(false);
                }}
              >
                Вопросы расследования
              </button>
              <button
                disabled={game.phase !== "DISCUSSION"}
                onClick={() => {
                  setInformation("finish");
                  setMenu(false);
                }}
              >
                Предложить завершить расследование
              </button>
              <label>
                <input
                  type="checkbox"
                  checked={confirmPublish}
                  onChange={(e) => {
                    setConfirmPublish(e.target.checked);
                    try {
                      localStorage.setItem(
                        "detective.confirmPublish",
                        String(e.target.checked),
                      );
                    } catch {
                      /* Local preference only. */
                    }
                  }}
                />
                Подтверждать публикацию
              </label>
              <small>В колоде: {game.deck_count}</small>
            </section>
          )}
        </div>,
        document.body,
      )}
      <section
        ref={discard}
        className="board-hud discard-zone"
        data-testid="discard-zone"
        aria-label="Закрытый сброс"
      >
        <span>Закрытый сброс</span>
        <strong data-testid="discard-count">{game.discarded_count}</strong>
        <small>Перетащите карту</small>
      </section>
      {!final && (
        <section
          className="board-hud private-hand"
          aria-label="Ваша личная рука"
        >
          <header>
            <h2>
              Мои карты <small>{game.private.hand.length}</small>
            </h2>
            <span>{myTurn ? "Ваш ход" : "Ожидайте своего хода"}</span>
          </header>
          <div className="hand-cards">
            {game.private.hand.map((card) => (
              <div
                className="hand-card-wrapper"
                key={card.id}
                onPointerDown={(event) => {
                  if (
                    !myTurn ||
                    event.button !== 0 ||
                    (event.target as HTMLElement).closest("button")
                  )
                    return;
                  capture(event);
                  setDrag({ kind: "hand", card, screen: point(event) });
                }}
              >
                <Card card={card} code={room.code} assets={assets} personal />
                <button className="hand-read" onClick={() => setReading(card)}>
                  Читать
                </button>
              </div>
            ))}
          </div>
          {!game.private.hand.length && (
            <small>Все личные карты разыграны</small>
          )}
        </section>
      )}
      {drag?.kind === "hand" && (
        <div
          className="card-ghost"
          data-testid="card-ghost"
          style={{ left: drag.screen.x, top: drag.screen.y }}
        >
          <Card card={drag.card} code={room.code} assets={assets} personal />
        </div>
      )}
      {(game.phase === "INTRO" || final) && (
        <div className="phase-overlay">{phasePanel}</div>
      )}
      {(information || reading || note) && (
        <div className="board-modal-backdrop">
          <section
            className="board-modal"
            role="dialog"
            aria-modal="true"
            aria-label={
              reading ? "Просмотр карты" : note ? "Заметка" : "Материалы дела"
            }
          >
            <button
              className="modal-close secondary"
              aria-label="Закрыть окно"
              onClick={() => {
                setInformation(null);
                setReading(null);
                setNote(null);
              }}
            >
              Закрыть
            </button>
            {reading ? (
              <Card
                card={reading}
                code={room.code}
                assets={assets}
                personal={game.private.hand.some((c) => c.id === reading.id)}
              />
            ) : note ? (
              <form
                onSubmit={async (e) => {
                  e.preventDefault();
                  if (!note.text.trim() || locked) return;
                  setNoteFailure(false);
                  await controller.command(
                    "board_mutation",
                    note.object
                      ? {
                          op: "note_update",
                          object_id: note.object.id,
                          expected_version: note.object.version,
                          x: note.x,
                          y: note.y,
                          text: note.text,
                        }
                      : {
                          op: "note_create",
                          object_id: crypto.randomUUID(),
                          x: note.x,
                          y: note.y,
                          text: note.text,
                        },
                  );
                  const result = controller.getSnapshot();
                  if (result.error || result.pending) setNoteFailure(true);
                  else setNote(null);
                }}
              >
                <h2>Заметка</h2>
                {noteFailure && (
                  <p role="alert">
                    {state.error ??
                      "Результат сохранения пока неизвестен. Закройте это окно и используйте повтор сохранённого запроса."}{" "}
                    Ваш текст сохранён в этом окне. При конфликте скопируйте его
                    перед закрытием и откройте актуальную заметку заново.
                  </p>
                )}

                <textarea
                  aria-label="Текст заметки"
                  maxLength={1000}
                  value={note.text}
                  onChange={(e) => setNote({ ...note, text: e.target.value })}
                />
                <button disabled={locked || !note.text.trim()}>
                  Сохранить заметку
                </button>
              </form>
            ) : information === "description" ? (
              <>
                <h2>{game.case.meta.title}</h2>
                <p>{game.intro.text}</p>
                <p>{game.intro.public_goal}</p>
              </>
            ) : information === "questions" ? (
              <>
                <h2>Вопросы расследования</h2>
                {game.questions?.length ? (
                  game.questions.map((q) => <p key={q.id}>{q.text}</p>)
                ) : (
                  <p>Вопросы откроются после завершения обсуждения.</p>
                )}
              </>
            ) : (
              phasePanel
            )}
          </section>
        </div>
      )}
    </main>
  );
}
