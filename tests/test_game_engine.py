"""Domain tests: no server, database, network, cookies or real user data."""

import json
from dataclasses import FrozenInstanceError, asdict, replace
from pathlib import Path
from random import Random

import pytest
from detective.game_engine import (
    GameError,
    Phase,
    Player,
    PlayMode,
    Position,
    apply,
    create_game,
    dump_state,
    private_state_for,
    public_state,
    restore_state,
)
from detective.game_engine import commands as c
from detective.game_engine.invariants import check_invariants
from detective.game_engine.models import Answer, Hand, PublishedCard

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def manifest():
    return json.loads((ROOT / "docs/examples/demo-case.json").read_text())


def lobby(manifest, count=2):
    return create_game(
        "game-1", manifest, tuple(Player(f"p{i}", f"Игрок {i}") for i in range(count)), "p0"
    )


def advance(state, actor, command, **kwargs):
    return apply(state, actor, command, **kwargs).state


def started(manifest, count=2, seed=11):
    return advance(lobby(manifest, count), "p0", c.StartGame(), rng=Random(seed))


def playing(manifest, count=2, seed=11):
    return advance(started(manifest, count, seed), "p0", c.BeginPlay())


def drain(state, discard_ids=None):
    while state.phase == Phase.PLAYING:
        actor = state.active_player_id
        card_id = state.hand_for(actor)[0]
        mode = (
            PlayMode.DISCARD if discard_ids is None or card_id in discard_ids else PlayMode.REVEAL
        )
        state = advance(
            state,
            actor,
            c.PlayCard(card_id, mode, Position(25, -30) if mode == PlayMode.REVEAL else None),
        )
    return state


def answering(manifest):
    state = drain(playing(manifest))
    state = advance(state, "p1", c.ProposeFinish())
    return advance(state, "p0", c.ConfirmFinish())


def review(manifest):
    state = answering(manifest)
    for q in manifest["questions"]:
        state = advance(state, "p1", c.SelectAnswer(q["id"], q["options"][0]["id"]))
    return advance(state, "p0", c.SubmitAnswers())


def results(manifest):
    state = review(manifest)
    for q in manifest["questions"]:
        state = advance(state, "p0", c.ReviewAnswer(q["id"], True))
    return state


def rejected(state, actor, command, code):
    before = dump_state(state)
    with pytest.raises(GameError) as error:
        apply(state, actor, command)
    assert error.value.code == code
    assert dump_state(state) == before


@pytest.mark.parametrize("count", range(2, 9))
def test_deal(manifest, count):
    initial = lobby(manifest, count)
    state = advance(initial, "p0", c.StartGame(), rng=Random(42))
    size = 3 if count == 2 else 2
    assert state.phase == Phase.INTRO
    assert all(len(h.card_ids) == size for h in state.hands)
    assert len(state.deck) == len(manifest["play_cards"]) - size * count
    assert len(state.published) == 1
    assert state.published[0].card_id == manifest["start_card"]["id"]
    assert set(state.turn_order) == {p.id for p in state.players}
    assert state.active_player_id is None
    assert all(not h.card_ids for h in initial.hands)
    assert state == started(manifest, count, 42)
    check_invariants(state)


def test_shuffle_varies_and_production_default(manifest):
    assert started(manifest, seed=1) != started(manifest, seed=2)
    check_invariants(advance(lobby(manifest), "p0", c.StartGame()))


@pytest.mark.parametrize("count", [0, 1, 9])
def test_bad_player_count(manifest, count):
    with pytest.raises(GameError):
        lobby(manifest, count)


def test_scenario_player_range(manifest):
    manifest["meta"]["max_players"] = 3
    with pytest.raises(GameError, match="INVALID_PLAYER_COUNT"):
        lobby(manifest, 4)


def test_duplicate_and_absent_host(manifest):
    with pytest.raises(GameError, match="INVALID_PLAYERS"):
        create_game("g", manifest, (Player("p", "A"), Player("p", "B")), "p")
    with pytest.raises(GameError, match="INVALID_HOST"):
        create_game("g", manifest, (Player("p", "A"), Player("q", "B")), "absent")


@pytest.mark.parametrize("mode", list(PlayMode))
def test_play_atomic_draw_and_next(manifest, mode):
    state = playing(manifest)
    actor = state.active_player_id
    card_id = state.hand_for(actor)[0]
    before = dump_state(state)
    command = c.PlayCard(card_id, mode, Position(50, -10) if mode == PlayMode.REVEAL else None)
    transition = apply(state, actor, command)
    new = transition.state
    assert dump_state(state) == before
    assert new.revision == state.revision + 1
    assert new.deck == state.deck[1:]
    assert new.hand_for(actor) == (*state.hand_for(actor)[1:], state.deck[0])
    assert new.active_player_id == state.turn_order[1]
    assert len(transition.events) == 1
    assert set(asdict(transition.events[0])) == {"revision", "phase"}
    if mode == PlayMode.REVEAL:
        assert new.published[-1] == PublishedCard(card_id, Position(50, -10))
        assert not new.discarded
    else:
        assert new.discarded == (card_id,)
        for player in state.players:
            view = private_state_for(new, player.id)
            assert card_id not in json.dumps(view)
    rejected(new, actor, command, "NOT_YOUR_TURN")
    next_actor = new.active_player_id
    new = advance(new, next_actor, c.PlayCard(new.hand_for(next_actor)[0], PlayMode.DISCARD))
    rejected(new, actor, command, "CARD_NOT_IN_HAND")


@pytest.mark.parametrize("mode", list(PlayMode))
def test_cancel(manifest, mode):
    state = playing(manifest)
    actor = state.active_player_id
    transition = apply(
        state,
        actor,
        c.PlayCard(
            state.hand_for(actor)[0],
            mode,
            Position(0, 0) if mode == PlayMode.REVEAL else None,
            confirmed=False,
        ),
    )
    assert transition.state is state
    assert transition.events == ()


def test_turn_and_ownership(manifest):
    state = playing(manifest)
    actor = state.active_player_id
    other = next(p.id for p in state.players if p.id != actor)
    rejected(state, other, c.PlayCard(state.hand_for(other)[0], PlayMode.DISCARD), "NOT_YOUR_TURN")
    rejected(
        state, actor, c.PlayCard(state.hand_for(other)[0], PlayMode.DISCARD), "CARD_NOT_IN_HAND"
    )
    rejected(state, "unknown", c.BeginPlay(), "NOT_A_MEMBER")
    rejected(state, actor, c.PlayCard("absent", PlayMode.DISCARD), "CARD_NOT_IN_HAND")
    # Commands have no actor/owner fields that a payload can override.
    with pytest.raises(TypeError):
        c.PlayCard("card", PlayMode.DISCARD, actor_id=other)


def test_invalid_play_payload(manifest):
    state = playing(manifest)
    actor = state.active_player_id
    card = state.hand_for(actor)[0]
    for command, code in [
        (c.PlayCard(card, "reveal", Position(0, 0)), "INVALID_COMMAND"),
        (c.PlayCard(card, PlayMode.REVEAL), "INVALID_POSITION"),
        (c.PlayCard(card, PlayMode.REVEAL, Position(float("nan"), 0)), "INVALID_POSITION"),
        (c.PlayCard(card, PlayMode.REVEAL, Position(0, float("inf"))), "INVALID_POSITION"),
        (c.PlayCard(card, PlayMode.REVEAL, Position(10**1000, 0)), "INVALID_POSITION"),
        (c.PlayCard(card, PlayMode.DISCARD, Position(0, 0)), "INVALID_POSITION"),
        (c.PlayCard(card, PlayMode.DISCARD, confirmed="yes"), "INVALID_COMMAND"),
    ]:
        rejected(state, actor, command, code)
    rejected(state, actor, object(), "UNKNOWN_COMMAND")


def test_phase_permissions_and_no_shortcuts(manifest):
    state = lobby(manifest)
    rejected(state, "p1", c.StartGame(), "HOST_ONLY")
    rejected(state, "p0", c.BeginPlay(), "WRONG_PHASE")
    state = started(manifest)
    rejected(state, "p0", c.StartGame(), "WRONG_PHASE")
    rejected(state, "p1", c.BeginPlay(), "HOST_ONLY")
    rejected(state, "p0", c.PlayCard(state.hand_for("p0")[0], PlayMode.DISCARD), "WRONG_PHASE")
    state = advance(state, "p0", c.BeginPlay())
    for command in [
        c.ProposeFinish(),
        c.ConfirmFinish(),
        c.SubmitAnswers(),
        c.ReviewAnswer("q1", True),
        c.FinishGame(),
        c.SelectAnswer("q1", "a"),
    ]:
        rejected(state, "p0", command, "WRONG_PHASE")
    state = drain(state)
    assert state.phase == Phase.DISCUSSION
    assert not state.deck and all(not h.card_ids for h in state.hands)
    rejected(state, "p0", c.ConfirmFinish(), "NO_PROPOSAL")
    state = advance(state, "p1", c.ProposeFinish())
    rejected(state, "p0", c.ProposeFinish(), "ALREADY_PROPOSED")
    rejected(state, "p1", c.ConfirmFinish(), "HOST_ONLY")
    state = advance(state, "p0", c.ConfirmFinish())
    assert state.phase == Phase.ANSWERING


def test_empty_deck_empty_hands_and_skip(manifest):
    state = playing(manifest, 8)
    assert not state.deck
    first = state.turn_order[0]
    second = state.turn_order[1]
    # A valid persisted mid-game distribution with one exhausted hand.
    emptied = state.hand_for(second)
    state = replace(
        state,
        hands=tuple(Hand(h.player_id) if h.player_id == second else h for h in state.hands),
        discarded=emptied,
    )
    check_invariants(state)
    state = advance(state, first, c.PlayCard(state.hand_for(first)[0], PlayMode.DISCARD))
    assert state.active_player_id == state.turn_order[2]
    assert len(state.hand_for(first)) == 1
    state = drain(state)
    assert state.phase == Phase.DISCUSSION and state.active_player_id is None
    rejected(state, first, c.PlayCard("absent", PlayMode.DISCARD), "WRONG_PHASE")


def test_team_answers_lock_and_review(manifest):
    state = answering(manifest)
    rejected(state, "p0", c.SubmitAnswers(), "ANSWERS_INCOMPLETE")
    rejected(state, "p1", c.SubmitAnswers(), "HOST_ONLY")
    rejected(state, "p0", c.SelectAnswer("missing", "a"), "UNKNOWN_QUESTION")
    rejected(state, "p0", c.SelectAnswer("q1", "e"), "UNKNOWN_OPTION")
    state = advance(state, "p1", c.SelectAnswer("q1", "a"))
    state = advance(state, "p0", c.SelectAnswer("q1", "b"))
    assert state.answers == (Answer("q1", "b"),)
    for question in manifest["questions"][1:]:
        state = advance(state, "p1", c.SelectAnswer(question["id"], "a"))
    state = advance(state, "p0", c.SubmitAnswers())
    assert state.phase == Phase.REVIEW
    rejected(state, "p1", c.ReviewAnswer("q1", True), "HOST_ONLY")
    rejected(state, "p0", c.ReviewAnswer("q1", "yes"), "INVALID_REVIEW")
    rejected(state, "p0", c.ReviewAnswer("bad", True), "UNKNOWN_QUESTION")
    rejected(state, "p0", c.SelectAnswer("q1", "a"), "WRONG_PHASE")
    rejected(state, "p0", c.SubmitAnswers(), "WRONG_PHASE")
    state = advance(state, "p0", c.ReviewAnswer("q1", False))
    state = advance(state, "p0", c.ReviewAnswer("q1", True))
    for question in manifest["questions"][1:]:
        state = advance(state, "p0", c.ReviewAnswer(question["id"], True))
    assert state.phase == Phase.RESULTS
    # Award follows human review even though selected options disagree with the key.
    assert state.result.correct_answers == 4
    assert state.result.score == 8
    rejected(state, "p1", c.FinishGame(), "HOST_ONLY")
    state = advance(state, "p0", c.FinishGame())
    assert state.phase == Phase.FINISHED
    assert public_state(state)["solution"] == manifest["solution"]
    for command in [c.StartGame(), c.FinishGame(), c.ReviewAnswer("q1", False)]:
        rejected(state, "p0", command, "WRONG_PHASE")


def test_score_acceptance_example_and_negative(manifest):
    # Two irrelevant cards published; five total discards.
    irrelevant = {c["id"] for c in manifest["play_cards"] if c["role"] == "red_herring"}
    discarded = set(sorted(irrelevant)[:4]) | {"clue-01"}
    state = drain(playing(manifest), discarded)
    state = advance(state, "p0", c.ProposeFinish())
    state = advance(state, "p0", c.ConfirmFinish())
    for q in manifest["questions"]:
        state = advance(state, "p1", c.SelectAnswer(q["id"], "a"))
    state = advance(state, "p0", c.SubmitAnswers())
    for q in manifest["questions"]:
        state = advance(state, "p0", c.ReviewAnswer(q["id"], True))
    assert state.result.score == 3
    assert state.result.irrelevant_revealed == 2
    assert state.result.missing_discards == 1
    assert state.result.discarded == 5
    # No discards and no correct answers must produce an un-clamped negative score.
    state = drain(playing(manifest), set())
    state = advance(state, "p0", c.ProposeFinish())
    state = advance(state, "p0", c.ConfirmFinish())
    for q in manifest["questions"]:
        state = advance(state, "p0", c.SelectAnswer(q["id"], "a"))
    state = advance(state, "p0", c.SubmitAnswers())
    for q in manifest["questions"]:
        state = advance(state, "p0", c.ReviewAnswer(q["id"], False))
    assert state.result.score == -24  # six false leads + 6*3 missing discards


def keys(value):
    if isinstance(value, dict):
        return set(value) | set().union(*(keys(v) for v in value.values()))
    if isinstance(value, list):
        return set().union(*(keys(v) for v in value))
    return set()


@pytest.mark.parametrize("factory", [lobby, started, playing, answering, review, results])
def test_projections_and_restore_every_phase(manifest, factory):
    state = factory(manifest)
    assert restore_state(dump_state(state)) == state
    assert "manifest_json" not in keys(public_state(state))
    rejected(state, "outsider", c.StartGame(), "NOT_A_MEMBER")
    with pytest.raises(GameError, match="NOT_A_MEMBER"):
        private_state_for(state, "outsider")
    public = public_state(state)
    for player in state.players:
        view = private_state_for(state, player.id)
        assert [card["id"] for card in view["private"]["hand"]] == list(state.hand_for(player.id))
        if state.phase != Phase.RESULTS:
            assert not {"solution", "role", "logic_notes", "supports_questions"} & keys(view)
            if state.phase == Phase.REVIEW and player.id == state.host_id:
                assert "correct_option_id" in keys(view["private"])
                assert "correct_option_id" not in keys(public)
            else:
                assert not {"correct_option_id", "explanation", "evidence_card_ids"} & keys(view)
            serialized = json.dumps(view, ensure_ascii=False)
            for hand in state.hands:
                if hand.player_id != player.id:
                    for card_id in hand.card_ids:
                        card = next(c for c in manifest["play_cards"] if c["id"] == card_id)
                        assert card_id not in serialized and card["title"] not in serialized
    assert "private" not in public
    # Projections are detached; caller changes cannot mutate domain state.
    before = dump_state(state)
    view["players"].clear()
    view["case"]["meta"].clear()
    assert dump_state(state) == before


def test_discussion_and_finished_restore(manifest):
    for state in [drain(playing(manifest)), advance(results(manifest), "p0", c.FinishGame())]:
        restored = restore_state(dump_state(state))
        assert restored == state
        for player in state.players:
            assert private_state_for(restored, player.id) == private_state_for(state, player.id)


def test_restore_then_continue_exactly_once(manifest):
    original = playing(manifest)
    actor = original.active_player_id
    cmd = c.PlayCard(original.hand_for(actor)[0], PlayMode.DISCARD)
    played = advance(original, actor, cmd)
    restored = restore_state(dump_state(played))
    assert restored.hand_for(actor) == played.hand_for(actor)
    assert restored.active_player_id == played.active_player_id
    rejected(restored, actor, cmd, "NOT_YOUR_TURN")
    assert drain(restored) == drain(played)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.update(state_version=2),
        lambda d: d["state"].update(revision=True),
        lambda d: d["state"].update(phase="FAKE"),
        lambda d: d["state"].update(active_player_id="absent"),
        lambda d: d["state"].update(host_id="absent"),
        lambda d: d["state"]["deck"].append(d["state"]["deck"][0]),
        lambda d: d["state"]["hands"][0].update(player_id="absent"),
        lambda d: d["state"].update(result={}),
        lambda d: d["state"].update(turn_order=["p0", "p0"]),
        lambda d: d["state"].update(unknown="value"),
    ],
)
def test_corrupt_saves_rejected(manifest, mutate):
    data = json.loads(dump_state(playing(manifest)))
    mutate(data)
    with pytest.raises(GameError, match="INVALID_SAVED_STATE"):
        restore_state(json.dumps(data))


@pytest.mark.parametrize("data", ["{", "[]", "null", '{"a":1,"a":2}'])
def test_bad_save(data):
    with pytest.raises(GameError, match="INVALID_SAVED_STATE"):
        restore_state(data)


def test_immutable_input_and_no_secret_repr(manifest):
    state = playing(manifest)
    saved = dump_state(state)
    manifest["solution"]["full_story"] = "changed"
    assert dump_state(state) == saved
    with pytest.raises(FrozenInstanceError):
        state.host_id = "other"
    assert "clue-" not in repr(state) + repr(state.scenario) + repr(state.hands)


@pytest.mark.parametrize("count", range(2, 9))
def test_generated_games_preserve_properties(manifest, count):
    # Generated operation sequences: 20 seeds * 7 player counts * every card play.
    for seed in range(20):
        state = playing(manifest, count, seed)
        random = Random(seed)
        played = set()
        while state.phase == Phase.PLAYING:
            actor = state.active_player_id
            card_id = random.choice(state.hand_for(actor))
            mode = random.choice(list(PlayMode))
            cmd = c.PlayCard(
                card_id, mode, Position(seed, -seed) if mode == PlayMode.REVEAL else None
            )
            before = dump_state(state)
            cancelled = apply(state, actor, replace(cmd, confirmed=False))
            assert cancelled.state is state and not cancelled.events
            old_order = state.turn_order
            state = advance(state, actor, cmd)
            assert card_id not in played
            played.add(card_id)
            check_invariants(state)
            assert state.turn_order == old_order
            assert restore_state(dump_state(state)) == state
            assert before != dump_state(state)
            if state.phase == Phase.PLAYING:
                index = old_order.index(actor)
                expected = next(
                    old_order[(index + offset) % count]
                    for offset in range(1, count + 1)
                    if state.hand_for(old_order[(index + offset) % count])
                )
                assert state.active_player_id == expected
        assert len(played) == len(manifest["play_cards"])


def test_games_are_isolated(manifest):
    first = playing(manifest)
    second = playing(manifest, seed=42)
    before = dump_state(second)
    drain(first)
    assert dump_state(second) == before


def test_private_images_and_discarded_content(manifest):
    manifest["assets"] = [
        {
            "id": "private-photo",
            "path": "assets/private-photo.png",
            "mime": "image/png",
            "alt": "PRIVATE ALT SENTINEL",
        }
    ]
    manifest["play_cards"][0]["blocks"] = [
        {"type": "image", "asset_id": "private-photo", "caption": "PRIVATE CAPTION SENTINEL"},
        {"type": "text", "segments": [{"text": "SHAREABLE SENTINEL", "shareable": "highlight"}]},
    ]
    state = playing(manifest)
    # Use a seed for which this card has been dealt, rather than manufacturing client state.
    for seed in range(100):
        candidate = playing(manifest, seed=seed)
        if any("clue-01" in h.card_ids for h in candidate.hands):
            state = candidate
            break
    owner = next(h.player_id for h in state.hands if "clue-01" in h.card_ids)
    other = next(p.id for p in state.players if p.id != owner)
    assert private_state_for(state, owner)["private"]["assets"] == manifest["assets"]
    for view in (public_state(state), private_state_for(state, other)):
        serialized = json.dumps(view)
        assert "private-photo" not in serialized
        assert "SENTINEL" not in serialized
    # Reach the owner's turn, discard the photo, ensure even its owner loses all contents.
    if state.active_player_id != owner:
        actor = state.active_player_id
        state = advance(state, actor, c.PlayCard(state.hand_for(actor)[0], PlayMode.DISCARD))
    state = advance(state, owner, c.PlayCard("clue-01", PlayMode.DISCARD))
    for player in state.players:
        serialized = json.dumps(private_state_for(state, player.id))
        assert "private-photo" not in serialized and "SENTINEL" not in serialized


def test_minimum_six_card_scenario(manifest):
    manifest["play_cards"] = manifest["play_cards"][-6:]
    manifest["meta"]["max_players"] = 2
    for question in manifest["questions"]:
        question["evidence_card_ids"] = ["clue-11", "clue-12"]
    state = playing(manifest)
    assert not state.deck and all(len(h.card_ids) == 3 for h in state.hands)
    assert drain(state).phase == Phase.DISCUSSION


def test_invalid_state_invariants_are_detected(manifest):
    state = playing(manifest)
    invalid = [
        replace(state, deck=(*state.deck, state.deck[0])),
        replace(state, deck=state.deck[1:]),
        replace(state, published=()),
        replace(state, active_player_id=None),
        replace(state, phase=Phase.DISCUSSION, active_player_id=None),
        replace(state, answers=(Answer("q1", "a"),)),
        replace(state, finish_proposed_by="p0"),
        replace(state, hands=tuple(Hand(h.player_id) for h in state.hands)),
    ]
    for broken in invalid:
        with pytest.raises(GameError):
            check_invariants(broken)
    result = results(manifest)
    with pytest.raises(GameError, match="INVALID_RESULT"):
        check_invariants(replace(result, result=replace(result.result, score=999)))
