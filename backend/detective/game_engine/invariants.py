"""Checks used at construction, restoration and both sides of every transition."""

import math
from collections import Counter

from .models import GameError, GameState, Phase, Position
from .scoring import calculate_result


def require(condition: bool, code: str) -> None:
    if not condition:
        raise GameError(code)


def valid_position(position: Position) -> bool:
    try:
        return all(type(v) in (int, float) and math.isfinite(v) for v in (position.x, position.y))
    except OverflowError:
        return False


def check_invariants(state: GameState) -> None:
    case = state.scenario.manifest()
    ids = tuple(player.id for player in state.players)
    require(isinstance(state.phase, Phase), "INVALID_STATE")
    require(
        bool(state.game_id) and type(state.revision) is int and state.revision >= 0, "INVALID_STATE"
    )
    require(2 <= len(ids) <= 8 and len(set(ids)) == len(ids) and all(ids), "INVALID_PLAYERS")
    require(
        case["meta"]["min_players"] <= len(ids) <= case["meta"]["max_players"],
        "INVALID_PLAYER_COUNT",
    )
    require(state.host_id in ids, "INVALID_HOST")
    require(tuple(h.player_id for h in state.hands) == ids, "INVALID_HAND_OWNERS")
    require(state.finish_proposed_by is None or state.finish_proposed_by in ids, "INVALID_PROPOSAL")
    all_cards = [
        *state.deck,
        *state.discarded,
        *(card for hand in state.hands for card in hand.card_ids),
        *(card.card_id for card in state.published),
    ]
    expected = [card["id"] for card in case["play_cards"]]
    if state.phase != Phase.LOBBY:
        expected.append(case["start_card"]["id"])
        require(
            sum(c.card_id == case["start_card"]["id"] for c in state.published) == 1,
            "INVALID_START_CARD",
        )
        require(
            len(state.turn_order) == len(ids) and set(state.turn_order) == set(ids),
            "INVALID_TURN_ORDER",
        )
    else:
        require(
            not state.turn_order
            and not state.published
            and not state.discarded
            and all(not h.card_ids for h in state.hands),
            "INVALID_LOBBY",
        )
    require(Counter(all_cards) == Counter(expected), "INVALID_CARD_ZONES")
    require(all(valid_position(card.position) for card in state.published), "INVALID_POSITION")
    if state.phase == Phase.PLAYING:
        require(state.active_player_id in ids, "INVALID_ACTIVE_PLAYER")
        require(bool(state.hand_for(state.active_player_id or "")), "EMPTY_ACTIVE_HAND")
    else:
        require(state.active_player_id is None, "INVALID_ACTIVE_PLAYER")
    late = (Phase.DISCUSSION, Phase.ANSWERING, Phase.REVIEW, Phase.RESULTS, Phase.FINISHED)
    if state.phase in late:
        require(not state.deck and all(not h.card_ids for h in state.hands), "CARDS_REMAIN")
    if state.phase == Phase.INTRO:
        size = 3 if len(ids) == 2 else 2
        require(
            all(len(h.card_ids) == size for h in state.hands)
            and not state.discarded
            and len(state.published) == 1,
            "INVALID_DEAL",
        )
    questions = {q["id"]: {o["id"] for o in q["options"]} for q in case["questions"]}
    answer_ids = [a.question_id for a in state.answers]
    require(len(answer_ids) == len(set(answer_ids)), "DUPLICATE_ANSWER")
    for answer in state.answers:
        require(
            answer.question_id in questions and answer.option_id in questions[answer.question_id],
            "INVALID_ANSWER",
        )
        require(
            answer.reviewed_correct is None or type(answer.reviewed_correct) is bool,
            "INVALID_REVIEW",
        )
    if state.phase not in (Phase.ANSWERING, Phase.REVIEW, Phase.RESULTS, Phase.FINISHED):
        require(not state.answers, "EARLY_ANSWERS")
    if state.phase in (Phase.REVIEW, Phase.RESULTS, Phase.FINISHED):
        require(set(answer_ids) == set(questions), "ANSWERS_INCOMPLETE")
    elif state.answers:
        require(all(a.reviewed_correct is None for a in state.answers), "EARLY_REVIEW")
    if state.phase in (Phase.RESULTS, Phase.FINISHED):
        require(state.result == calculate_result(state), "INVALID_RESULT")
    else:
        require(state.result is None, "EARLY_RESULT")
    if state.phase == Phase.REVIEW:
        require(any(a.reviewed_correct is None for a in state.answers), "REVIEW_ALREADY_COMPLETE")
    if state.phase in (Phase.ANSWERING, Phase.REVIEW, Phase.RESULTS, Phase.FINISHED):
        require(state.finish_proposed_by is not None, "MISSING_PROPOSAL")
    elif state.phase != Phase.DISCUSSION:
        require(state.finish_proposed_by is None, "EARLY_PROPOSAL")
