import { useState } from "react";
import type { Player } from "../contracts";
export function Players({
  players,
  hostId,
  ownId,
  activeId,
  online,
}: {
  players: Player[];
  hostId: string;
  ownId: string;
  activeId?: string | null;
  online: Record<string, boolean>;
}) {
  const [collapsed, setCollapsed] = useState(false);
  return (
    <section className="players-panel" aria-label="Участники">
      <div className="panel-title">
        <h2>
          <button
            className="players-toggle"
            data-testid="players-toggle"
            aria-expanded={!collapsed}
            onClick={() => setCollapsed(!collapsed)}
          >
            Игроки {collapsed ? "▸" : "▾"}
          </button>
        </h2>
        <span>{players.length}/8</span>
      </div>
      <ul hidden={collapsed}>
        {players.map((player, i) => (
          <li
            key={player.id}
            className={activeId === player.id ? "active-player" : ""}
            data-member-id={player.id}
          >
            <span className="avatar" aria-hidden="true">
              {i + 1}
            </span>
            <div>
              <strong>
                {player.nickname}
                {player.id === ownId ? " · вы" : ""}
              </strong>
              <small>
                {player.id === hostId ? "Ведущий · " : ""}
                {online[player.id] === undefined
                  ? "Статус уточняется"
                  : online[player.id]
                    ? "На связи"
                    : "Не в сети"}
                {player.hand_count !== undefined
                  ? ` · карт: ${player.hand_count}`
                  : ""}
              </small>
            </div>
            {activeId === player.id && (
              <span
                className="turn-dot"
                title="Текущий ход"
                aria-label="Текущий ход"
              />
            )}
          </li>
        ))}
      </ul>
    </section>
  );
}
