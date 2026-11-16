"""Allowlist projections. These are the ONLY domain objects intended for clients."""

from dataclasses import asdict
from typing import Any

from .invariants import require
from .models import GameState, Phase


def _card(card: dict[str, Any]) -> dict[str, Any]:
    return {key: card[key] for key in ("id", "title", "blocks", "kind") if key in card}


def public_state(state: GameState) -> dict[str, Any]:
    """Room-public data; the transport still must check membership."""
    case = state.scenario.manifest()
    cards = {card["id"]: card for card in [case["start_card"], *case["play_cards"]]}
    result_visible = state.phase in (Phase.RESULTS, Phase.FINISHED)
    view: dict[str, Any] = {
        "game_id": state.game_id,
        "revision": state.revision,
        "phase": state.phase.value,
        "host_id": state.host_id,
        "players": [
            {"id": p.id, "nickname": p.nickname, "hand_count": len(state.hand_for(p.id))}
            for p in state.players
        ],
        "active_player_id": state.active_player_id,
        "turn_order": list(state.turn_order),
        "deck_count": len(state.deck),
        "discarded_count": len(state.discarded),
        "case": {"id": case["id"], "meta": case["meta"]},
        "published": [
            {"card": _card(cards[c.card_id]), "position": asdict(c.position)}
            for c in state.published
        ],
        "finish_proposed_by": state.finish_proposed_by,
        "answers": [
            {
                "question_id": a.question_id,
                "option_id": a.option_id,
                **({"reviewed_correct": a.reviewed_correct} if result_visible else {}),
            }
            for a in state.answers
        ],
    }
    if state.phase != Phase.LOBBY:
        view["intro"] = case["intro"]
    if state.phase in (Phase.ANSWERING, Phase.REVIEW, Phase.RESULTS, Phase.FINISHED):
        view["questions"] = [
            {key: q[key] for key in ("id", "text", "options")} for q in case["questions"]
        ]
    if result_visible:
        view["solution"] = case["solution"]
        view["answer_key"] = _key(case)
        view["cards"] = [case["start_card"], *case["play_cards"]]
        view["discarded"] = list(state.discarded)
        view["result"] = asdict(state.result) if state.result is not None else None
    # Only disclose asset declarations referenced by visible cards, never the complete catalogue.
    visible_cards = [c["card"] for c in view["published"]]
    if result_visible:
        visible_cards = view["cards"]
    view["assets"] = _assets(case, visible_cards)
    return view


def _key(case: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {key: q[key] for key in ("id", "correct_option_id", "explanation", "evidence_card_ids")}
        for q in case["questions"]
    ]


def _assets(case: dict[str, Any], cards: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ids = {
        block["asset_id"] for card in cards for block in card["blocks"] if block["type"] == "image"
    }
    return [asset for asset in case["assets"] if asset["id"] in ids]


def private_state_for(state: GameState, member_id: str) -> dict[str, Any]:
    require(member_id in {p.id for p in state.players}, "NOT_A_MEMBER")
    case = state.scenario.manifest()
    cards = {card["id"]: card for card in case["play_cards"]}
    view = public_state(state)
    hand = [_card(cards[card_id]) for card_id in state.hand_for(member_id)]
    view["private"] = {"hand": hand, "assets": _assets(case, hand)}
    if state.phase == Phase.REVIEW and member_id == state.host_id:
        view["private"]["answer_key"] = _key(case)
        view["private"]["reviewed_answers"] = [
            {"question_id": a.question_id, "reviewed_correct": a.reviewed_correct}
            for a in state.answers
        ]
    return view
