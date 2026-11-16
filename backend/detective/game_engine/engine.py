"""Pure rule transitions. Authentication/transactions/idempotency belong to the service."""

from dataclasses import replace
from random import Random, SystemRandom
from typing import Any

from . import commands as c
from .invariants import check_invariants, require, valid_position
from .models import (
    Answer,
    GameError,
    GameState,
    Hand,
    Phase,
    Player,
    PlayMode,
    Position,
    PublishedCard,
    Scenario,
    StateChanged,
    Transition,
)
from .scoring import calculate_result


def create_game(
    game_id: str, manifest: dict[str, Any], players: tuple[Player, ...], host_id: str
) -> GameState:
    scenario = Scenario.from_manifest(manifest)
    state = GameState(
        game_id,
        scenario,
        players,
        host_id,
        hands=tuple(Hand(p.id) for p in players),
        deck=tuple(card["id"] for card in manifest["play_cards"]),
    )
    check_invariants(state)
    return state


def apply(
    state: GameState, actor_id: str, command: c.Command, *, rng: Random | None = None
) -> Transition:
    """actor_id must be resolved by the trusted caller, never taken from a payload.

    Inject Random(seed) for reproducible initial shuffling. Production uses SystemRandom.
    An unconfirmed PlayCard represents a cancelled/uncommitted gesture and is a no-op.
    """
    check_invariants(state)
    require(actor_id in {p.id for p in state.players}, "NOT_A_MEMBER")
    required_phase: dict[type[object], Phase] = {
        c.StartGame: Phase.LOBBY,
        c.BeginPlay: Phase.INTRO,
        c.PlayCard: Phase.PLAYING,
        c.ProposeFinish: Phase.DISCUSSION,
        c.ConfirmFinish: Phase.DISCUSSION,
        c.SelectAnswer: Phase.ANSWERING,
        c.SubmitAnswers: Phase.ANSWERING,
        c.ReviewAnswer: Phase.REVIEW,
        c.FinishGame: Phase.RESULTS,
    }
    require(type(command) in required_phase, "UNKNOWN_COMMAND")
    require(state.phase == required_phase[type(command)], "WRONG_PHASE")
    if isinstance(
        command,
        c.StartGame
        | c.BeginPlay
        | c.ConfirmFinish
        | c.SubmitAnswers
        | c.ReviewAnswer
        | c.FinishGame,
    ):
        require(actor_id == state.host_id, "HOST_ONLY")
    new = state
    match command:
        case c.StartGame():
            deck, order = list(state.deck), [p.id for p in state.players]
            random = rng if rng is not None else SystemRandom()
            random.shuffle(deck)
            random.shuffle(order)
            size = 3 if len(order) == 2 else 2
            hands: dict[str, list[str]] = {p.id: [] for p in state.players}
            for _ in range(size):
                for player_id in order:
                    hands[player_id].append(deck.pop(0))
            start_id = state.scenario.manifest()["start_card"]["id"]
            new = replace(
                state,
                phase=Phase.INTRO,
                deck=tuple(deck),
                turn_order=tuple(order),
                hands=tuple(Hand(p.id, tuple(hands[p.id])) for p in state.players),
                published=(PublishedCard(start_id, Position(0, 0)),),
            )
        case c.BeginPlay():
            new = replace(state, phase=Phase.PLAYING, active_player_id=state.turn_order[0])
        case c.PlayCard():
            require(actor_id == state.active_player_id, "NOT_YOUR_TURN")
            require(command.card_id in state.hand_for(actor_id), "CARD_NOT_IN_HAND")
            require(
                isinstance(command.mode, PlayMode) and type(command.confirmed) is bool,
                "INVALID_COMMAND",
            )
            if command.mode == PlayMode.REVEAL:
                require(
                    isinstance(command.position, Position) and valid_position(command.position),
                    "INVALID_POSITION",
                )
            else:
                require(command.position is None, "INVALID_POSITION")
            if not command.confirmed:
                return Transition(state, ())
            new = _play(state, actor_id, command)
        case c.ProposeFinish():
            require(state.finish_proposed_by is None, "ALREADY_PROPOSED")
            new = replace(state, finish_proposed_by=actor_id)
        case c.ConfirmFinish():
            require(state.finish_proposed_by is not None, "NO_PROPOSAL")
            new = replace(state, phase=Phase.ANSWERING)
        case c.SelectAnswer():
            require(
                type(command.question_id) is str and type(command.option_id) is str,
                "INVALID_COMMAND",
            )
            questions = {q["id"]: q for q in state.scenario.manifest()["questions"]}
            require(command.question_id in questions, "UNKNOWN_QUESTION")
            require(
                command.option_id in {o["id"] for o in questions[command.question_id]["options"]},
                "UNKNOWN_OPTION",
            )
            answers = tuple(a for a in state.answers if a.question_id != command.question_id)
            new = replace(state, answers=(*answers, Answer(command.question_id, command.option_id)))
        case c.SubmitAnswers():
            require(
                len(state.answers) == len(state.scenario.manifest()["questions"]),
                "ANSWERS_INCOMPLETE",
            )
            new = replace(state, phase=Phase.REVIEW)
        case c.ReviewAnswer():
            require(type(command.question_id) is str, "INVALID_COMMAND")
            require(type(command.correct) is bool, "INVALID_REVIEW")
            require(
                command.question_id in {a.question_id for a in state.answers}, "UNKNOWN_QUESTION"
            )
            answers = tuple(
                replace(a, reviewed_correct=command.correct)
                if a.question_id == command.question_id
                else a
                for a in state.answers
            )
            new = replace(state, answers=answers)
            if all(a.reviewed_correct is not None for a in answers):
                new = replace(new, phase=Phase.RESULTS, result=calculate_result(new))
        case c.FinishGame():
            new = replace(state, phase=Phase.FINISHED)
        case _:
            raise GameError("UNKNOWN_COMMAND")
    new = replace(new, revision=state.revision + 1)
    check_invariants(new)
    return Transition(new, (StateChanged(new.revision, new.phase),))


def _play(state: GameState, actor_id: str, command: c.PlayCard) -> GameState:
    cards = tuple(card for card in state.hand_for(actor_id) if card != command.card_id)
    if state.deck:
        cards = (*cards, state.deck[0])
    hands = tuple(Hand(h.player_id, cards) if h.player_id == actor_id else h for h in state.hands)
    published, discarded = state.published, state.discarded
    if command.mode == PlayMode.REVEAL:
        assert command.position is not None  # checked before applying any change
        published = (*published, PublishedCard(command.card_id, command.position))
    else:
        discarded = (*discarded, command.card_id)
    next_id = None
    index = state.turn_order.index(actor_id)
    for offset in range(1, len(state.turn_order) + 1):
        candidate = state.turn_order[(index + offset) % len(state.turn_order)]
        if any(h.player_id == candidate and h.card_ids for h in hands):
            next_id = candidate
            break
    return replace(
        state,
        hands=hands,
        deck=state.deck[1:],
        published=published,
        discarded=discarded,
        active_player_id=next_id,
        phase=Phase.PLAYING if next_id is not None else Phase.DISCUSSION,
    )
