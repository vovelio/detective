import { useEffect, useState, type FormEvent } from "react";
import { parseRoom, validNickname } from "./api";
import type { AppState, SessionController } from "./session";
import type { GameSnapshot } from "./contracts";
import { BoardGame } from "./board/BoardGame";
import { CaseImport } from "./components/CaseImport";
import { Players } from "./components/Players";
import type { Confirmation } from "./components/Dialog";

export function Home({
  state,
  controller,
}: {
  state: AppState;
  controller: SessionController;
}) {
  const [nickname, setNickname] = useState("");
  const [code, setCode] = useState(state.invitation);
  useEffect(() => {
    setCode(state.invitation);
  }, [state.invitation]);
  const valid = validNickname(nickname);
  const disabled = state.busy || state.loading || state.sessionLost;
  function submit(event: FormEvent, join: boolean) {
    event.preventDefault();
    if (valid)
      void (join
        ? controller.join(nickname, code)
        : controller.create(nickname));
  }
  return (
    <main className="home">
      <section className="home-intro">
        <span className="eyebrow">
          Кооперативное расследование · 2–8 игроков
        </span>
        <h1>
          У каждого —<br />
          часть истории.
        </h1>
        <p>Соберите улики, сопоставьте версии и найдите ответ вместе.</p>
        <div className="home-note">
          <span className="case-symbol" aria-hidden="true">
            Д
          </span>
          <p>
            Обсуждайте открытые сведения.
            <br />
            Личные карты остаются только у вас.
          </p>
        </div>
        <p className="muted small">
          Голосовая связь — во внешнем приложении. Можно выбрать учебное дело
          или добавить собственный сценарий.
        </p>
      </section>
      <section className="entry-panel">
        <span className="eyebrow">Начать расследование</span>
        <h2>Соберите свою команду</h2>
        <form onSubmit={(e) => submit(e, false)}>
          <label htmlFor="nickname">Ваше имя</label>
          <input
            id="nickname"
            autoComplete="nickname"
            value={nickname}
            maxLength={80}
            onChange={(e) => setNickname(e.target.value)}
            placeholder="Как вас назвать?"
            disabled={disabled}
            aria-describedby="nickname-help"
          />
          <small id="nickname-help" className="muted">
            От 2 до 20 символов
          </small>
          <button
            type="submit"
            data-testid="create-room"
            disabled={disabled || !valid}
          >
            Создать комнату <span aria-hidden="true">↗</span>
          </button>
        </form>
        <div className="divider">или войдите по приглашению</div>
        <form onSubmit={(e) => submit(e, true)}>
          <label htmlFor="room-code">Ссылка или код комнаты</label>
          <input
            id="room-code"
            value={code}
            onChange={(e) => setCode(e.target.value)}
            autoComplete="off"
            placeholder="Вставьте приглашение"
            disabled={disabled}
          />
          <button
            className="secondary"
            type="submit"
            data-testid="join-room"
            disabled={disabled || !valid || !parseRoom(code)}
          >
            Присоединиться
          </button>
        </form>
        {state.rooms.length > 0 && (
          <section className="resume-rooms">
            <h3>Ваши комнаты</h3>
            {state.rooms.map((room) => (
              <button
                className="text-button"
                key={room.code}
                disabled={disabled}
                onClick={() => void controller.openRoom(room.code)}
              >
                Продолжить · {room.code}
              </button>
            ))}
          </section>
        )}
        <button
          className="text-button"
          disabled={state.busy || state.loading}
          onClick={() => void controller.bootstrap()}
        >
          Обновить список моих комнат
        </button>
      </section>
    </main>
  );
}

export function Lobby({
  state,
  controller,
}: {
  state: AppState;
  controller: SessionController;
}) {
  const room = state.room!;
  const host = room.member_id === room.host_id;
  const busy =
    state.busy ||
    state.pending ||
    !["connected", "syncing"].includes(state.connection);
  const selected = state.cases.find((c) => c.id === room.selected_case_id);
  const enough =
    selected &&
    room.players.length >= selected.meta.min_players &&
    room.players.length <= selected.meta.max_players;
  const [copied, setCopied] = useState(false);
  const invitation = `${location.origin}/rooms/${room.code}`;
  return (
    <main className="lobby">
      <div className="lobby-heading">
        <span className="eyebrow">Комната {room.code}</span>
        <h1>Перед началом дела</h1>
        <p className="muted">Пригласите друзей и выберите расследование.</p>
      </div>
      <div className="lobby-grid">
        <Players
          players={room.players}
          hostId={room.host_id}
          ownId={room.member_id}
          online={state.online}
        />
        <section className="case-panel">
          <div className="panel-title">
            <h2>Детективное дело</h2>
            <span className="pill">
              {host ? "Вы выбираете" : "Выбирает ведущий"}
            </span>
          </div>
          <label className="sr-only" htmlFor="scenario">
            Выберите дело
          </label>
          <select
            id="scenario"
            value={room.selected_case_id ?? ""}
            disabled={!host || busy}
            onChange={(e) => void controller.selectCase(e.target.value)}
          >
            <option value="" disabled>
              Выберите дело
            </option>
            {state.cases.map((item) => (
              <option key={item.id} value={item.id}>
                {item.meta.title}
              </option>
            ))}
          </select>
          {selected ? (
            <div className="case-summary">
              <span className="eyebrow">
                {selected.meta.estimated_minutes} минут ·{" "}
                {selected.meta.min_players}–{selected.meta.max_players} игроков
              </span>
              <h3>{selected.meta.title}</h3>
              <p>{selected.meta.summary}</p>
              <small className="muted">Автор: {selected.meta.author}</small>
            </div>
          ) : (
            <p className="empty">Ведущий пока не выбрал дело.</p>
          )}
          {host ? (
            <CaseImport state={state} controller={controller} disabled={busy} />
          ) : (
            <p className="muted small">
              Свои дела может добавлять ведущий до начала расследования.
            </p>
          )}
          {host ? (
            <>
              <button
                data-testid="start-game"
                disabled={busy || !enough}
                onClick={() => void controller.startGame()}
              >
                Начать расследование
              </button>
              {!enough && (
                <small className="muted">
                  {!selected
                    ? "Выберите дело, чтобы начать."
                    : `Для этого дела нужны ${selected.meta.min_players}–${selected.meta.max_players} игроков.`}
                </small>
              )}
            </>
          ) : (
            <p className="notice">Ожидаем начала расследования от ведущего.</p>
          )}
        </section>
        <section className="invitation-panel">
          <span className="eyebrow">Команда собирается здесь</span>
          <h2>Пригласите друзей</h2>
          <label className="sr-only" htmlFor="invitation-link">
            Пригласительная ссылка
          </label>
          <input
            id="invitation-link"
            value={invitation}
            readOnly
            onFocus={(e) => e.target.select()}
          />
          <button
            className="secondary"
            onClick={() => {
              void navigator.clipboard
                .writeText(invitation)
                .then(() => setCopied(true))
                .catch(() => {
                  document
                    .querySelector<HTMLInputElement>("#invitation-link")
                    ?.select();
                });
            }}
          >
            {copied ? "Ссылка скопирована" : "Копировать ссылку"}
          </button>
          <p className="muted small">
            Все входят без регистрации. После старта новые участники не смогут
            присоединиться.
          </p>
        </section>
      </div>
    </main>
  );
}

export function Game({
  game,
  state,
  controller,
  confirm,
}: {
  game: GameSnapshot;
  state: AppState;
  controller: SessionController;
  confirm: (value: Confirmation) => void;
}) {
  const host = state.room!.member_id === game.host_id;
  const locked =
    state.busy ||
    state.pending ||
    !["connected", "syncing"].includes(state.connection);
  return (
    <BoardGame
      game={game}
      state={state}
      controller={controller}
      confirm={confirm}
      phasePanel={
        <PhasePanel
          game={game}
          host={host}
          locked={locked}
          controller={controller}
          confirm={confirm}
        />
      }
    />
  );
}

function PhasePanel({
  game,
  host,
  locked,
  controller,
  confirm,
}: {
  game: GameSnapshot;
  host: boolean;
  locked: boolean;
  controller: SessionController;
  confirm: (value: Confirmation) => void;
}) {
  if (game.phase === "INTRO")
    return (
      <section className="phase-panel intro-panel">
        <span className="eyebrow">Завязка дела</span>
        <h2>С чего всё началось</h2>
        <p>{game.intro.text}</p>
        <p className="notice">{game.intro.public_goal}</p>
        {host ? (
          <button
            disabled={locked}
            onClick={() => void controller.command("begin_play", {})}
          >
            Начать игру
          </button>
        ) : (
          <p className="muted">Прочитайте вступление. Ведущий начнёт игру.</p>
        )}
      </section>
    );
  if (game.phase === "DISCUSSION")
    return (
      <section className="phase-panel">
        <h2>Время собрать общую версию</h2>
        <p>
          Все карты разыграны. Обсудите опубликованные улики и вспомните
          сброшенные сведения.
        </p>
        {!game.finish_proposed_by ? (
          <button
            disabled={locked}
            onClick={() => void controller.command("propose_finish", {})}
          >
            Предложить завершить расследование
          </button>
        ) : host ? (
          <>
            <p className="notice">Поступило предложение перейти к вопросам.</p>
            <button
              disabled={locked}
              onClick={() => void controller.command("confirm_finish", {})}
            >
              Перейти к вопросам
            </button>
          </>
        ) : (
          <p className="notice">Ожидаем подтверждения ведущего.</p>
        )}
      </section>
    );
  if (game.phase === "ANSWERING" || game.phase === "REVIEW") {
    const reviewing = game.phase === "REVIEW";
    const complete = game.questions?.every((q) =>
      game.answers.some((a) => a.question_id === q.id),
    );
    return (
      <section className="phase-panel questions-panel">
        <h2>
          {reviewing ? "Ответы зафиксированы" : "Один ответ от всей команды"}
        </h2>
        <p className="muted">
          {reviewing
            ? host
              ? "Проверьте каждый ответ вручную по ключу. Последняя отметка откроет результаты."
              : "Ведущий проверяет ответы. Изменить выбор больше нельзя."
            : "Выбор любого участника виден всей команде. Ведущий подтвердит окончательные ответы."}
        </p>
        {game.questions?.map((q, index) => {
          const selected = game.answers.find(
            (a) => a.question_id === q.id,
          )?.option_id;
          const key = game.private.answer_key?.find((k) => k.id === q.id);
          const mark = game.private.reviewed_answers?.find(
            (a) => a.question_id === q.id,
          )?.reviewed_correct;
          return (
            <fieldset key={q.id} data-question={q.id}>
              <legend>
                {index + 1}. {q.text}
              </legend>
              {q.options.map((option) => (
                <label
                  className={`answer-option ${selected === option.id ? "selected" : ""}`}
                  key={option.id}
                >
                  <input
                    type="radio"
                    name={q.id}
                    value={option.id}
                    checked={selected === option.id}
                    disabled={reviewing || locked}
                    onChange={() =>
                      void controller.command("answer", {
                        question_id: q.id,
                        option_id: option.id,
                      })
                    }
                  />
                  {option.text}
                </label>
              ))}
              {reviewing && host && key && (
                <div className="review-key">
                  <strong>
                    Ключ автора:{" "}
                    {
                      q.options.find((o) => o.id === key.correct_option_id)
                        ?.text
                    }
                  </strong>
                  <p>{key.explanation}</p>
                  <small>
                    Опорные улики: {key.evidence_card_ids.join(", ")}
                  </small>
                  <div className="review-actions">
                    <button
                      className={mark === true ? "marked" : "secondary"}
                      aria-pressed={mark === true}
                      disabled={locked}
                      onClick={() =>
                        void controller.command("review_answer", {
                          question_id: q.id,
                          correct: true,
                        })
                      }
                    >
                      Верно
                    </button>
                    <button
                      className={mark === false ? "marked" : "secondary"}
                      aria-pressed={mark === false}
                      disabled={locked}
                      onClick={() =>
                        void controller.command("review_answer", {
                          question_id: q.id,
                          correct: false,
                        })
                      }
                    >
                      Неверно
                    </button>
                    <span className="muted">
                      {mark === true
                        ? "Отмечено: верно"
                        : mark === false
                          ? "Отмечено: неверно"
                          : "Ожидает проверки"}
                    </span>
                  </div>
                </div>
              )}
            </fieldset>
          );
        })}
        {!reviewing && host && (
          <button
            disabled={locked || !complete}
            onClick={() =>
              confirm({
                title: "Зафиксировать ответы команды?",
                text: "После подтверждения изменить выбор нельзя. Откроется ручная проверка ведущего.",
                confirm: "Зафиксировать ответы",
                run: () => controller.command("submit_answers", {}),
              })
            }
          >
            Отправить ответы команды
          </button>
        )}
      </section>
    );
  }
  if (game.phase === "RESULTS" || game.phase === "FINISHED")
    return (
      <section className="phase-panel result-panel">
        <span className="eyebrow">Итоги расследования</span>
        <h2>
          {game.phase === "FINISHED" ? "Партия завершена" : "История раскрыта"}
        </h2>
        <div className="score-block">
          <strong data-testid="score">{game.result?.score}</strong>
          <span>очков команды</span>
        </div>
        <p className="score-formula">
          {game.result?.answer_points} за ответы −{" "}
          {game.result?.irrelevant_penalty} за лишние улики −{" "}
          {game.result?.missing_discard_penalty} за недостаток сброса
        </p>
        <h3>Что произошло</h3>
        <p>{game.solution?.full_story}</p>
        <ol className="timeline">
          {game.solution?.timeline.map((event, i) => (
            <li key={i}>
              <strong>{event.when}</strong> {event.event}
            </li>
          ))}
        </ol>
        {game.questions?.map((q) => {
          const answer = game.answers.find((a) => a.question_id === q.id);
          const key = game.answer_key?.find((k) => k.id === q.id);
          return (
            <div key={q.id} className="result-answer">
              <h3>{q.text}</h3>
              <p>
                Ответ команды:{" "}
                {q.options.find((o) => o.id === answer?.option_id)?.text} ·{" "}
                {answer?.reviewed_correct ? "Засчитан" : "Не засчитан"}
              </p>
              <p className="muted">
                Верный вариант:{" "}
                {q.options.find((o) => o.id === key?.correct_option_id)?.text}.{" "}
                {key?.explanation}
              </p>
            </div>
          );
        })}
        <h3>Разбор улик</h3>
        <p>{game.solution?.reveal_notes}</p>
        <details>
          <summary>Показать значимость всех карт</summary>
          {game.cards
            ?.filter((c) => c.role)
            .map((card) => (
              <div className="result-clue" key={card.id}>
                <strong>{card.title}</strong>
                <span className="pill">
                  {card.role === "red_herring"
                    ? "Ложный след"
                    : card.role === "essential"
                      ? "Ключевая улика"
                      : "Подтверждение"}
                </span>
                <p>{card.logic_notes}</p>
              </div>
            ))}
        </details>
        {game.phase === "RESULTS" && host && (
          <button
            disabled={locked}
            onClick={() => void controller.command("finish_game", {})}
          >
            Завершить партию
          </button>
        )}
        <button className="secondary" onClick={() => controller.home()}>
          На главную
        </button>
      </section>
    );
  return null;
}
