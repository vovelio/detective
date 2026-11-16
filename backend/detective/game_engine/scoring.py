"""Manual review drives correctness; the key never decides the awarded points."""

from .models import GameError, GameState, Result


def calculate_result(state: GameState) -> Result:
    case = state.scenario.manifest()
    if len(state.answers) != len(case["questions"]) or any(
        answer.reviewed_correct is None for answer in state.answers
    ):
        raise GameError("REVIEW_INCOMPLETE")
    roles = {card["id"]: card["role"] for card in case["play_cards"]}
    irrelevant = sum(roles.get(card.card_id) == "red_herring" for card in state.published)
    correct = sum(answer.reviewed_correct is True for answer in state.answers)
    scoring = case["scoring"]
    missing = max(0, scoring["minimum_discards"] - len(state.discarded))
    points = correct * scoring["points_per_correct"]
    irrelevant_penalty = irrelevant * scoring["penalty_per_irrelevant_revealed"]
    missing_penalty = missing * scoring["penalty_per_missing_discard"]
    return Result(
        correct,
        irrelevant,
        len(state.discarded),
        missing,
        points,
        irrelevant_penalty,
        missing_penalty,
        points - irrelevant_penalty - missing_penalty,
    )
