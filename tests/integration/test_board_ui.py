"""Independent two-player board acceptance through real Chrome pointer events."""

import asyncio
import json
import os
import socket
import subprocess

import httpx
import pytest
import pytest_asyncio
from websockets.asyncio.client import connect

from .test_ui import UI, frontend  # noqa: F401

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def browser_ui(tmp_path):
    executable = os.environ.get("DETECTIVE_CHROME_BIN")
    if not executable:
        pytest.skip("Set DETECTIVE_CHROME_BIN for board browser E2E")
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    process = subprocess.Popen(
        [
            executable,
            "--headless=new",
            f"--remote-debugging-port={port}",
            "--remote-debugging-address=127.0.0.1",
            f"--user-data-dir={tmp_path / 'board-chrome'}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-background-networking",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        async with httpx.AsyncClient() as probe:
            for _ in range(150):
                try:
                    reply = await probe.get(f"http://127.0.0.1:{port}/json/version")
                    if reply.status_code == 200:
                        uri = reply.json()["webSocketDebuggerUrl"]
                        break
                except httpx.TransportError:
                    pass
                await asyncio.sleep(0.05)
            else:
                raise RuntimeError("Board test Chrome did not start")
        async with connect(uri, proxy=None, max_size=2**22) as channel:
            yield UI(channel)
    finally:
        if process.poll() is None:
            process.terminate()
            await asyncio.to_thread(process.wait, timeout=5)


async def test_two_player_board_drag_tools_preview_and_recovery(
    pg,
    frontend,  # noqa: F811 — imported pytest fixture
    live,
    browser_ui,
):
    ui = browser_ui
    host, _ = await ui.page_ui(frontend)
    await ui.fill(host, "#nickname", "Ведущий")
    await ui.click(host, "[data-testid=create-room]")
    await ui.ready(host)
    code = await ui.evaluate(host, "location.pathname.split('/')[2]")
    guest, _ = await ui.page_ui(frontend + "/rooms/" + code)
    await ui.fill(guest, "#nickname", "ДлинноеИмяИгрока")
    await ui.click(guest, "[data-testid=join-room]")
    await ui.ready(guest)
    await ui.wait(host, "document.querySelectorAll('[data-member-id]').length===2")
    await ui.evaluate(
        host,
        "document.querySelector('#scenario').value='envelope-demo'; "
        "document.querySelector('#scenario').dispatchEvent("
        "new Event('change',{bubbles:true})); true",
    )
    await ui.ready(host)
    await ui.click(host, "[data-testid=start-game]")
    await ui.ready(host, "INTRO")
    await ui.button(host, "Начать игру")
    for page in (host, guest):
        await ui.ready(page, "PLAYING")

    async def style(page, selector="[data-testid=board-world]"):
        return await ui.evaluate(
            page, f"document.querySelector({json.dumps(selector)}).style.transform"
        )

    async def snapshot(page):
        # Only returned safe board payload; no private state is returned to the test output.
        return await ui.evaluate(
            page,
            f"fetch('/api/v1/rooms/{code}/snapshot').then(r=>r.json()).then(s=>({{revision:s.revision,board:s.board}}))",
        )

    async def tool(page, label):
        await ui.click(page, f'[data-testid=board-toolbar] button[aria-label="{label}"]')

    # HUD geometry and independent camera, including pan initiated on a card.
    viewport = await ui.box(host, "[data-testid=board-viewport]")
    hand = await ui.box(host, ".private-hand")
    players = await ui.box(host, ".board-players")
    discard = await ui.box(host, "[data-testid=discard-zone]")
    assert viewport["width"] > 1400 and viewport["height"] > 850
    assert hand["x"] > 700 and hand["y"] > 600
    assert players["x"] < 50 and players["width"] < 300
    assert discard["width"] < 270 and discard["height"] < 350
    await ui.click(host, "[data-testid=players-toggle]")
    assert await ui.evaluate(host, "document.querySelector('.players-panel ul').hidden")
    await ui.click(host, "[data-testid=players-toggle]")

    async def grid(page):
        return await ui.evaluate(
            page,
            """(()=>{
              const surface=document.querySelector('[data-testid=board-viewport]');
              const css=getComputedStyle(surface);
              const matrix=new DOMMatrix(getComputedStyle(
                document.querySelector('[data-testid=board-world]')).transform);
              const size=css.backgroundSize.split(' ').map(parseFloat);
              const pos=css.backgroundPosition.split(' ').map(parseFloat);
              return {size,pos,x:matrix.e,y:matrix.f,zoom:matrix.a,
                image:css.backgroundImage,repeat:css.backgroundRepeat};
            })()""",
        )

    async def assert_grid(page):
        value = await grid(page)
        step = 24 * value["zoom"]
        assert value["size"] == pytest.approx([step, step], abs=0.001)
        assert value["pos"] == pytest.approx(
            [value["x"] - step / 2, value["y"] - step / 2], abs=0.001
        )
        assert value["repeat"] == "repeat"
        assert f"{value['zoom']:g}px" in value["image"]
        return value

    initial_host, initial_guest = await style(host), await style(guest)
    grid_before = await assert_grid(host)
    guest_grid = await assert_grid(guest)
    await tool(host, "Перемещение доски")
    card = await ui.box(host, "[data-testid=board-card]")
    await ui.drag(
        host,
        {"x": card["x"] + 50, "y": card["y"] + 50},
        {"x": card["x"] + 120, "y": card["y"] + 110},
    )
    assert await style(host) != initial_host and await style(guest) == initial_guest
    grid_panned = await assert_grid(host)
    assert await grid(guest) == guest_grid
    moved_card = await ui.box(host, "[data-testid=board-card]")
    assert grid_panned["pos"][0] - grid_before["pos"][0] == pytest.approx(
        moved_card["x"] - card["x"], abs=0.01
    )
    assert grid_panned["pos"][1] - grid_before["pos"][1] == pytest.approx(
        moved_card["y"] - card["y"], abs=0.01
    )
    assert await ui.box(host, ".private-hand") == hand
    assert await ui.box(host, ".board-players") == players
    pointer_world = (
        "(()=>{const m=new DOMMatrix(getComputedStyle("
        "document.querySelector('[data-testid=board-world]')).transform);"
        "return {x:(500-m.e)/m.a,y:(450-m.f)/m.d}})()"
    )
    anchor_before = await ui.evaluate(host, pointer_world)
    await ui.mouse(host, "mouseWheel", {"x": 500, "y": 450}, deltaX=0, deltaY=-120)
    await asyncio.sleep(0.1)
    assert await style(guest) == initial_guest
    anchor_after = await ui.evaluate(host, pointer_world)
    grid_zoomed = await assert_grid(host)
    assert grid_zoomed["size"][0] > grid_panned["size"][0]
    assert await grid(guest) == guest_grid
    assert abs(anchor_after["x"] - anchor_before["x"]) < 0.01
    assert abs(anchor_after["y"] - anchor_before["y"]) < 0.01
    assert await ui.box(host, ".private-hand") == hand
    await ui.mouse(host, "mouseMoved", {"x": 510, "y": 460})
    await ui.wait(guest, "!!document.querySelector('[data-testid=cursor]')")
    assert await ui.evaluate(
        guest, "!!document.querySelector('[data-testid=cursor]').textContent.trim()"
    )
    await ui.button(host, "К центру")
    centered = await style(host)
    assert "scale(1)" in centered
    await ui.button(host, "К центру")
    assert await style(host) == centered
    await tool(host, "Выбор")

    # Private ghost is local, no POST/revision/publication until release + confirmation.
    actor = await ui.active(host, guest)
    observer = guest if actor == host else host
    before = await ui.revision(actor)
    await ui.drop(actor, "reveal", release=False)
    assert await ui.evaluate(actor, "!!document.querySelector('[data-testid=card-ghost]')")
    assert await ui.evaluate(
        observer,
        "!document.querySelector('[data-testid=card-ghost]') && "
        "document.querySelectorAll('[data-testid=public-card]').length===1",
    )
    assert await ui.revision(observer) == before
    await ui.mouse(
        actor, "mouseReleased", {"x": 440, "y": 430}, button="left", buttons=0, clickCount=1
    )
    await ui.wait(actor, "!!document.querySelector('dialog[open]')")
    assert await ui.revision(observer) == before
    await ui.button(actor, "Отмена")
    assert await ui.revision(actor) == before
    await ui.card(actor, "reveal")
    await ui.button(actor, "Подтвердить публикацию")
    for page in (host, guest):
        await ui.wait_revision(page, before + 1)
    # World coordinates come from actual drop and camera matrix, not automatic center.
    assert await ui.evaluate(
        actor,
        "(()=>{const cards=[...document.querySelectorAll('[data-testid=board-card]')];"
        "return cards.some(c=>{const r=c.getBoundingClientRect();"
        "return Math.abs(r.x-440)<2&&Math.abs(r.y-430)<2})})()",
    )

    # Non-active player can drag a public card; observer sees ephemeral movement BEFORE commit.
    editor = actor  # publication handed the turn to the other player
    other = observer
    await tool(editor, "Выбор")
    selector = "[data-testid=board-card]"
    old = await ui.box(editor, selector)
    old_other = await ui.box(other, selector)
    db_before = await snapshot(editor)
    revision = await ui.revision(editor)
    start = {"x": old["x"] + 30, "y": old["y"] + 35}
    end = {"x": start["x"] + 110, "y": start["y"] + 60}
    await ui.drag(editor, start, end, release=False)
    await ui.wait(
        other,
        f"Math.abs(document.querySelector('[data-testid=board-card]').getBoundingClientRect().x-{old_other['x']})>5",
    )
    assert (await snapshot(other)) == db_before
    await ui.mouse(editor, "mouseReleased", end, button="left", buttons=0, clickCount=1)
    try:
        for page in (host, guest):
            await ui.wait_revision(page, revision + 1)
    except RuntimeError:
        info = await ui.evaluate(
            editor,
            "({revision:Number(document.querySelector('[data-phase]').dataset.revision),pending:!!document.querySelector('.pending-action'),error:document.querySelector('[role=alert]')?.textContent})",
        )
        raise RuntimeError(f"Public card final commit failed: {info}") from None
    assert (await snapshot(other))["board"] == (await snapshot(editor))["board"]
    # Rotation handle is shared and persisted.
    handle = await ui.box(editor, "[data-testid=rotation-handle]")
    revision = await ui.revision(editor)
    await ui.drag(
        editor,
        {"x": handle["x"] + handle["width"] / 2, "y": handle["y"] + handle["height"] / 2},
        {"x": end["x"] + 150, "y": end["y"] + 130},
    )
    await ui.wait_revision(other, revision + 1)
    assert await style(editor, selector) == await style(other, selector)

    # Shared free arrow, pencil and note, followed by eraser of each annotation type.
    for label, testid, start, end in (
        ("Стрелка", "board-arrow", {"x": 270, "y": 520}, {"x": 420, "y": 580}),
        ("Карандаш", "board-stroke", {"x": 260, "y": 610}, {"x": 430, "y": 650}),
    ):
        await ui.ready(editor)
        await tool(editor, label)
        revision = await ui.revision(editor)
        await ui.drag(editor, start, end)
        await ui.wait_revision(other, revision + 1)
        assert await ui.evaluate(
            other, f"document.querySelectorAll('[data-testid={testid}]').length===1"
        )
    await ui.ready(editor)
    await tool(editor, "Заметка")
    await ui.mouse(
        editor, "mousePressed", {"x": 300, "y": 750}, button="left", buttons=1, clickCount=1
    )
    await ui.mouse(
        editor, "mouseReleased", {"x": 300, "y": 750}, button="left", buttons=0, clickCount=1
    )
    await ui.fill(editor, 'textarea[aria-label="Текст заметки"]', "Общая тестовая заметка")
    revision = await ui.revision(editor)
    await ui.button(editor, "Сохранить заметку")
    await ui.wait_revision(other, revision + 1)
    assert await ui.evaluate(
        other, "document.querySelectorAll('[data-testid=board-note]').length===1"
    )
    # Selection drags annotations without opening the note editor; endpoints move separately.
    await tool(editor, "Выбор")
    for selector in (
        "[data-testid=board-note]",
        "[data-testid=board-arrow] line:nth-child(2)",
        "[data-testid=arrow-from]",
        "[data-testid=arrow-to]",
    ):
        await ui.ready(editor)
        before = (await snapshot(editor))["board"]
        bounds = await ui.box(editor, selector)
        start = {"x": bounds["x"] + bounds["width"] / 2, "y": bounds["y"] + bounds["height"] / 2}
        revision = await ui.revision(editor)
        await ui.drag(editor, start, {"x": start["x"] + 35, "y": start["y"] - 20})
        await ui.wait_revision(other, revision + 1)
        after = (await snapshot(other))["board"]
        kind = "note" if "note" in selector else "arrow"
        old = next(o for o in before["objects"] if o["kind"] == kind)["payload"]
        new = next(o for o in after["objects"] if o["kind"] == kind)["payload"]
        if kind == "note":
            assert new["x"] == old["x"] + 35 and new["y"] == old["y"] - 20
            assert new["text"] == old["text"]
            assert await ui.evaluate(editor, "!document.querySelector('textarea')")
        else:
            for key in ("from", "to"):
                moved = (
                    "arrow-from" not in selector
                    and "arrow-to" not in selector
                    or f"arrow-{key}" in selector
                )
                assert new[key] == (
                    {"x": old[key]["x"] + 35, "y": old[key]["y"] - 20} if moved else old[key]
                )
        assert (await snapshot(editor))["board"] == after
    # Independent editors of the same note: stale object version is explicit, draft retained.
    await ui.ready(editor)
    await tool(editor, "Выбор")
    await tool(other, "Выбор")
    await ui.click(editor, "[data-testid=board-note]")
    await ui.click(other, "[data-testid=board-note]")
    await ui.fill(editor, 'textarea[aria-label="Текст заметки"]', "Первое изменение")
    await ui.fill(other, 'textarea[aria-label="Текст заметки"]', "Второе изменение")
    revision = await ui.revision(editor)
    await ui.button(editor, "Сохранить заметку")
    await ui.wait_revision(other, revision + 1)
    await ui.button(other, "Сохранить заметку")
    await ui.wait(other, "!!document.querySelector('.board-modal [role=alert]')")
    assert await ui.evaluate(other, "document.querySelector('textarea').value==='Второе изменение'")
    assert await ui.revision(other) == revision + 1
    await ui.click(other, 'button[aria-label="Закрыть окно"]')
    assert await ui.evaluate(
        other, "document.querySelector('[data-testid=board-note]').textContent==='Первое изменение'"
    )
    saved = await snapshot(other)
    own = await ui.fingerprint(other)
    await ui.call("Page.reload", session=other)
    await ui.wait(other, "!!document.querySelector('[data-phase]')")
    await ui.ready(other, "PLAYING")
    assert await snapshot(other) == saved
    assert await ui.fingerprint(other) == own
    await live.stop()
    await live.launch()
    await ui.ready(editor, "PLAYING")
    await ui.ready(other, "PLAYING")
    assert await snapshot(other) == saved
    await tool(other, "Ластик")
    for testid in ("board-arrow", "board-stroke", "board-note"):
        await ui.ready(other)
        revision = await ui.revision(other)
        await ui.click(
            other,
            "[data-testid=arrow-to]" if testid == "board-arrow" else f"[data-testid={testid}]",
        )
        await ui.wait_revision(editor, revision + 1)
        assert await ui.evaluate(editor, f"!document.querySelector('[data-testid={testid}]')")
    assert await ui.evaluate(
        editor, "document.querySelectorAll('[data-testid=board-card]').length===2"
    )

    # Eraser cannot delete a public card or advance game state.
    revision = await ui.revision(other)
    await ui.click(other, "[data-testid=board-card]")
    assert await ui.revision(other) == revision
    assert await ui.evaluate(
        other, "document.querySelectorAll('[data-testid=board-card]').length===2"
    )
