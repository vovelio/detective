"""Full React UI game through real DOM controls, Vite, Uvicorn and PostgreSQL."""

import asyncio
import base64
import io
import json
import os
import socket
import subprocess
import zipfile

import httpx
import pytest
import pytest_asyncio
from websockets.asyncio.client import connect

from .conftest import ROOT
from .test_browser_sync import CDP

pytestmark = pytest.mark.asyncio


@pytest_asyncio.fixture
async def frontend(live):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    origin = f"http://127.0.0.1:{port}"
    await live.stop()
    live.env["ALLOWED_ORIGINS"] = live.origin + "," + origin
    await live.launch()
    process = subprocess.Popen(
        ["npm", "run", "dev", "--", "--port", str(port)],
        cwd=ROOT / "frontend",
        env={**os.environ, "DETECTIVE_API_TARGET": live.origin},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    try:
        async with httpx.AsyncClient() as probe:
            for _ in range(150):
                if process.poll() is not None:
                    raise RuntimeError("Vite exited before readiness")
                try:
                    if (await probe.get(origin)).status_code == 200:
                        break
                except httpx.TransportError:
                    pass
                await asyncio.sleep(0.05)
            else:
                raise RuntimeError("Vite did not become ready")
        yield origin
    finally:
        if process.poll() is None:
            import signal

            os.killpg(process.pid, signal.SIGTERM)
            await asyncio.to_thread(process.wait, timeout=5)


def make_upload_package(tmp_path, *, source_id="browser-import-demo"):
    source = tmp_path / "media.detective"
    if not source.exists():
        # Production Compose E2E has no operator-installed/DB fixture package.
        # Generate original fixture media locally, then upload it through the UI.
        from PIL import Image

        manifest = json.loads((ROOT / "docs/examples/demo-case.json").read_text())
        manifest["assets"] = []
        with zipfile.ZipFile(source, "w", zipfile.ZIP_DEFLATED) as archive:
            for index, card in enumerate([manifest["start_card"], *manifest["play_cards"]]):
                asset_id, name = f"photo-{index:02}", f"assets/photo-{index:02}.png"
                card["blocks"].append({"type": "image", "asset_id": asset_id, "caption": "Фото"})
                manifest["assets"].append(
                    {"id": asset_id, "path": name, "mime": "image/png", "alt": "Test image"}
                )
                image = io.BytesIO()
                Image.new("RGB", (2, 2), (index, 0, 0)).save(image, format="PNG")
                archive.writestr(name, image.getvalue())
            archive.writestr("case.json", json.dumps(manifest))
    destination = tmp_path / "browser-import.detective"
    with (
        zipfile.ZipFile(source) as incoming,
        zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as outgoing,
    ):
        for item in incoming.infolist():
            data = incoming.read(item.filename)
            if item.filename == "case.json":
                manifest = json.loads(data)
                manifest["id"] = source_id
                manifest["meta"]["title"] = "Загруженное учебное дело"
                data = json.dumps(manifest, ensure_ascii=False).encode()
            outgoing.writestr(item.filename, data)
    return destination


class UI(CDP):
    async def page_ui(self, url):
        context = (await self.call("Target.createBrowserContext"))["browserContextId"]
        target = (
            await self.call("Target.createTarget", {"url": url, "browserContextId": context})
        )["targetId"]
        session = (await self.call("Target.attachToTarget", {"targetId": target, "flatten": True}))[
            "sessionId"
        ]
        await self.call(
            "Emulation.setDeviceMetricsOverride",
            {"width": 1440, "height": 1000, "deviceScaleFactor": 1, "mobile": False},
            session,
        )
        await self.wait(
            session,
            "!!document.querySelector('#nickname') && "
            "!document.querySelector('#nickname').disabled",
        )
        return session, context

    async def fill(self, session, selector, value):
        await self.wait(session, f"!!document.querySelector({json.dumps(selector)})")
        await self.evaluate(
            session,
            f"document.querySelector({json.dumps(selector)}).focus(); "
            f"document.querySelector({json.dumps(selector)}).select(); true",
        )
        await self.call("Input.insertText", {"text": value}, session)

    async def click(self, session, selector):
        target = f"document.querySelector({json.dumps(selector)})"
        await self.wait(session, f"!!{target} && !{target}.disabled")
        await self.evaluate(
            session, f"{target}.scrollIntoView({{block:'center',inline:'center'}}); true"
        )
        point = await self.evaluate(
            session,
            f"(()=>{{const r={target}.getBoundingClientRect(); return "
            f"{{x:r.x+r.width/2,y:r.y+r.height/2}};}})()",
        )
        hit = await self.evaluate(
            session,
            f"(()=>{{const t={target}; "
            f"const e=document.elementFromPoint({point['x']},{point['y']});"
            "return {ok:t===e||t.contains(e),tag:e?.tagName,"
            "cls:typeof e?.className==='string'?e.className:''}})()",
        )
        if not hit["ok"]:
            raise RuntimeError(f"Control hit blocked: {selector} {hit}")
        await self.call("Input.dispatchMouseEvent", {"type": "mouseMoved", **point}, session)
        await self.call(
            "Input.dispatchMouseEvent",
            {"type": "mousePressed", "button": "left", "clickCount": 1, **point},
            session,
        )
        await self.call(
            "Input.dispatchMouseEvent",
            {"type": "mouseReleased", "button": "left", "clickCount": 1, **point},
            session,
        )

    async def button(self, session, label):
        # Tag a visible DOM control; no API command is issued by the test itself.
        text = json.dumps(label)
        await self.wait(
            session,
            f"""Array.from(document.querySelectorAll('button'))
              .some(b=>b.textContent.trim()==={text} && !b.disabled)""",
        )
        await self.evaluate(
            session,
            f"""document.querySelectorAll('[data-click-target]')
                .forEach(b=>b.removeAttribute('data-click-target'));
            Array.from(document.querySelectorAll('button'))
                .find(b=>b.textContent.trim()==={text} && !b.disabled)
                .setAttribute('data-click-target','yes'); true""",
        )
        await self.click(session, '[data-click-target="yes"]')

    async def ready(self, session, phase=None):
        await self.wait(
            session, "document.querySelector('.app-shell')?.getAttribute('aria-busy')==='false'"
        )
        await self.wait(
            session,
            "document.querySelector('[data-testid=connection-status]')?.textContent==='На связи'",
        )
        if phase:
            await self.wait(
                session,
                f"document.querySelector('[data-phase]')?.dataset.phase==={json.dumps(phase)}",
            )

    async def revision(self, session):
        return await self.evaluate(
            session, "Number(document.querySelector('[data-phase]').dataset.revision)"
        )

    async def active(self, host, guest):
        for session in (host, guest):
            await self.ready(session, "PLAYING")
        return (
            host
            if await self.evaluate(
                host,
                "document.querySelector('.private-hand header')?.textContent.includes('Ваш ход')",
            )
            else guest
        )

    async def wait_import(self, session, expected="success"):
        # Validation has a documented 10s worker budget and a 50s client timeout.
        # The generic 3s DOM wait is inappropriate for this network operation.
        for _ in range(500):
            state = await self.evaluate(
                session,
                "document.querySelector('[data-testid=import-state]')?.dataset.status",
            )
            if state == expected:
                return
            if state in ("success", "error", "uncertain"):
                raise RuntimeError(f"Import reached {state}, expected {expected}")
            await asyncio.sleep(0.1)
        raise RuntimeError("Import did not reach a terminal state within its client timeout")

    async def upload_file(self, session, path):
        await self.wait(session, "!!document.querySelector('[data-testid=import-file]')")
        document = await self.call("DOM.getDocument", {"depth": 1}, session)
        node = await self.call(
            "DOM.querySelector",
            {"nodeId": document["root"]["nodeId"], "selector": "[data-testid=import-file]"},
            session,
        )
        await self.call(
            "DOM.setFileInputFiles", {"nodeId": node["nodeId"], "files": [str(path)]}, session
        )

    async def box(self, session, selector):
        await self.wait(session, f"!!document.querySelector({json.dumps(selector)})")
        return await self.evaluate(
            session,
            f"(()=>{{const r=document.querySelector({json.dumps(selector)})"
            ".getBoundingClientRect(); return {x:r.x,y:r.y,width:r.width,height:r.height}})()",
        )

    async def mouse(self, session, kind, point, **extra):
        await self.call("Input.dispatchMouseEvent", {"type": kind, **point, **extra}, session)

    async def drag(self, session, start, end, release=True):
        await self.mouse(session, "mouseMoved", start)
        await self.mouse(session, "mousePressed", start, button="left", buttons=1, clickCount=1)
        for fraction in (0.2, 0.4, 0.6, 0.8, 1):
            await self.mouse(
                session,
                "mouseMoved",
                {k: start[k] + (end[k] - start[k]) * fraction for k in ("x", "y")},
                button="left",
                buttons=1,
            )
            await asyncio.sleep(0.035)
        if release:
            await self.mouse(session, "mouseReleased", end, button="left", buttons=0, clickCount=1)

    async def drop(self, session, mode, release=True):
        r = await self.box(session, "[data-testid=private-card]")
        start = {"x": r["x"] + 30, "y": r["y"] + 30}
        if mode == "discard":
            d = await self.box(session, "[data-testid=discard-zone]")
            end = {"x": d["x"] + d["width"] / 2, "y": d["y"] + d["height"] / 2}
        else:
            end = {"x": 440, "y": 430}
        await self.drag(session, start, end, False)
        await self.wait(session, "!!document.querySelector('[data-testid=card-ghost]')")
        if release:
            await self.mouse(session, "mouseReleased", end, button="left", buttons=0, clickCount=1)
        return end

    async def card(self, session, mode):
        await self.drop(session, mode)
        await self.wait(session, "!!document.querySelector('dialog[open]')")

    async def commit_card(self, session, mode):
        await self.card(session, mode)
        await self.button(
            session, "Подтвердить сброс" if mode == "discard" else "Подтвердить публикацию"
        )

    async def wait_revision(self, session, revision):
        await self.wait(
            session, f"Number(document.querySelector('[data-phase]')?.dataset.revision)>={revision}"
        )
        await self.ready(session)

    async def fingerprint(self, session):
        return await self.evaluate(
            session,
            """(async()=>{
            const cards=Array.from(document.querySelectorAll('[data-testid=private-card]'));
            return await Promise.all(cards.map(async c=>Array.from(new Uint8Array(await
                crypto.subtle.digest('SHA-256',new TextEncoder().encode(c.textContent))))
                .map(v=>v.toString(16).padStart(2,'0')).join('')));
        })()""",
        )

    async def event(self, method):
        for index, item in enumerate(self.events):
            if item.get("method") == method:
                return self.events.pop(index)["params"]
        while True:
            reply = json.loads(await asyncio.wait_for(self.socket.recv(), 5))
            if reply.get("method") == method:
                return reply["params"]
            if "method" in reply:
                self.events.append(reply)


async def full_game_scenario(ui, frontend, restart, tmp_path, checkpoint=None):
    """Shared DOM-only party for HTTP development and isolated HTTPS Compose.

    The optional checkpoint may back up/restore the isolated container stack.
    It must preserve cookie identity, board and hand before returning.
    """
    host, _ = await ui.page_ui(frontend)
    # Screenshots are allowed ONLY before any private card/key is in the page.
    screenshot = await ui.call("Page.captureScreenshot", {"format": "png"}, host)
    (tmp_path / "home-desktop.png").write_bytes(base64.b64decode(screenshot["data"]))
    await ui.call(
        "Emulation.setDeviceMetricsOverride",
        {"width": 390, "height": 844, "deviceScaleFactor": 1, "mobile": True},
        host,
    )
    assert await ui.evaluate(host, "document.documentElement.scrollWidth<=window.innerWidth")
    await ui.call(
        "Emulation.setDeviceMetricsOverride",
        {"width": 1440, "height": 1000, "deviceScaleFactor": 1, "mobile": False},
        host,
    )
    await ui.fill(host, "#nickname", "Ведущий")
    await ui.click(host, "[data-testid=create-room]")
    await ui.ready(host)
    code = await ui.evaluate(host, "location.pathname.split('/')[2]")
    guest, _ = await ui.page_ui(frontend + "/rooms/" + code)
    await ui.fill(guest, "#nickname", "Участник")
    await ui.click(guest, "[data-testid=join-room]")
    await ui.ready(guest)
    await ui.wait(host, "document.querySelectorAll('[data-member-id]').length===2")
    # The complete game uses a user-uploaded package, never operator CLI ingestion.
    package = make_upload_package(tmp_path)
    await ui.click(host, "[data-testid=import-open]")
    await ui.upload_file(host, package)
    await ui.click(host, "[data-testid=import-submit]")
    await ui.wait_import(host)
    await ui.click(host, "[data-testid=import-close]")
    await ui.wait(
        host,
        "Array.from(document.querySelector('#scenario').options).some("
        "o=>o.textContent.includes('Загруженное учебное дело'))",
    )
    await ui.evaluate(
        host,
        "const select=document.querySelector('#scenario'); "
        "select.value=Array.from(select.options).find("
        "o=>o.textContent.includes('Загруженное учебное дело')).value; "
        "select.dispatchEvent(new Event('change',{bubbles:true})); true",
    )
    await ui.ready(host)
    await ui.click(host, "[data-testid=start-game]")
    await ui.ready(host, "INTRO")
    await ui.ready(guest, "INTRO")
    await ui.button(host, "Начать игру")
    await ui.ready(host, "PLAYING")
    await ui.ready(guest, "PLAYING")
    first, second = await ui.fingerprint(host), await ui.fingerprint(guest)
    assert len(first) == len(second) == 3 and not set(first) & set(second)
    await ui.wait(
        host,
        "Array.from(document.querySelectorAll('[data-testid=private-card] "
        "img')).every(i=>i.complete && i.naturalWidth>0)",
    )
    private_asset = await ui.evaluate(
        host,
        f"fetch('/api/v1/rooms/{code}/snapshot').then(r=>r.json()).then(s=>s.private.assets[0].id)",
    )
    denied_asset = await ui.evaluate(
        guest,
        f"fetch('/api/v1/rooms/{code}/assets/' + {json.dumps(private_asset)}).then(r=>r.status)",
    )
    assert denied_asset == 404
    outsider, _ = await ui.page_ui(frontend + "/rooms/" + code)
    assert await ui.evaluate(
        outsider, "document.querySelectorAll('[data-testid=private-card]').length===0"
    )
    assert (
        await ui.evaluate(outsider, f"fetch('/api/v1/rooms/{code}/snapshot').then(r=>r.status)")
        == 403
    )
    actor = await ui.active(host, guest)
    before = await ui.revision(actor)
    await ui.card(actor, "reveal")
    await ui.button(actor, "Отмена")
    assert await ui.revision(actor) == before
    await ui.commit_card(actor, "reveal")
    for session in (host, guest):
        await ui.wait_revision(session, before + 1)
        assert await ui.evaluate(
            session, "document.querySelectorAll('[data-testid=public-card]').length===2"
        )
    actor = await ui.active(host, guest)
    before = await ui.revision(actor)
    await ui.card(actor, "discard")
    await ui.button(actor, "Отмена")
    assert await ui.revision(actor) == before
    await ui.commit_card(actor, "discard")
    for session in (host, guest):
        await ui.wait_revision(session, before + 1)
    # Publication confirmation can be disabled, while discard remains mandatory.
    actor = await ui.active(host, guest)
    await ui.click(actor, "[data-testid=settings]")
    await ui.wait(
        actor,
        "document.querySelector('[data-testid=settings]').getAttribute('aria-expanded')==='true'",
    )
    await ui.click(actor, ".settings-menu input")
    await ui.click(actor, "[data-testid=settings]")
    revision = await ui.revision(actor)
    await ui.card(actor, "discard")
    await ui.button(actor, "Отмена")
    assert await ui.revision(actor) == revision
    await ui.drop(actor, "reveal")
    for session in (host, guest):
        await ui.wait_revision(session, revision + 1)
    assert await ui.evaluate(actor, "!document.querySelector('dialog[open]')")
    # Existing board operations also work with an uploaded scenario.
    await ui.click(host, '[aria-label="Выбор"]')
    public = await ui.box(host, "[data-testid=board-card]")
    revision = await ui.revision(host)
    await ui.drag(
        host,
        {"x": public["x"] + 30, "y": public["y"] + 40},
        {"x": public["x"] + 80, "y": public["y"] + 80},
    )
    for session in (host, guest):
        await ui.wait_revision(session, revision + 1)
    board_position = await ui.evaluate(
        host,
        "(()=>{const c=document.querySelector('[data-testid=board-card]'); "
        "return [c.style.left,c.style.top,c.style.transform].join('|')})()",
    )
    own_hand = await ui.fingerprint(host)
    # Persisted imported package, room catalog, game and protected media survive restart.
    await restart()
    await ui.call("Page.reload", session=host)
    await ui.wait(host, "!!document.querySelector('[data-phase]')")
    await ui.ready(host, "PLAYING")
    unchanged = await ui.fingerprint(host) == own_hand
    assert unchanged
    assert (
        await ui.evaluate(
            host,
            "(()=>{const c=document.querySelector('[data-testid=board-card]'); "
            "return [c.style.left,c.style.top,c.style.transform].join('|')})()",
        )
        == board_position
    )
    await ui.wait(
        host,
        "Array.from(document.querySelectorAll('[data-testid=private-card] "
        "img')).every(i=>i.complete && i.naturalWidth>0)",
    )
    if checkpoint is not None:
        await checkpoint(ui, host, guest, code)
    # Offline observer misses a turn and automatically refreshes on return.
    actor = await ui.active(host, guest)
    observer = guest if actor == host else host
    before = await ui.revision(actor)
    await ui.call("Network.enable", session=observer)
    await ui.call(
        "Network.emulateNetworkConditions",
        {"offline": True, "latency": 0, "downloadThroughput": 0, "uploadThroughput": 0},
        observer,
    )
    await ui.wait(
        observer,
        "document.querySelector('[data-testid=connection-status]')?.textContent==='Переподключение…'",
    )
    await ui.commit_card(actor, "discard")
    await ui.wait_revision(actor, before + 1)
    await ui.call(
        "Network.emulateNetworkConditions",
        {"offline": False, "latency": 0, "downloadThroughput": -1, "uploadThroughput": -1},
        observer,
    )
    await ui.wait_revision(observer, before + 1)
    # Drop a response AFTER the API committed; F5 must retain the exact pending intent.
    actor = await ui.active(host, guest)
    before = await ui.revision(actor)
    await ui.call(
        "Fetch.enable",
        {"patterns": [{"urlPattern": "*/api/v1/rooms/*/actions", "requestStage": "Response"}]},
        actor,
    )
    await ui.commit_card(actor, "discard")
    paused = await ui.event("Fetch.requestPaused")
    await ui.call(
        "Fetch.failRequest",
        {"requestId": paused["requestId"], "errorReason": "ConnectionReset"},
        actor,
    )
    await ui.call("Fetch.disable", session=actor)
    await ui.wait(
        actor,
        "!!sessionStorage.getItem('detective.pendingAction.v1:'+location.pathname.split('/')[2])",
    )
    await ui.wait_revision(actor, before + 1)
    await ui.call("Page.reload", session=actor)
    await ui.wait(actor, "!!document.querySelector('[data-phase]')")
    await ui.ready(actor, "PLAYING")
    assert await ui.evaluate(
        actor,
        "sessionStorage.getItem('detective.pendingAction.v1:'+location.pathname.split('/')[2])!==null",
    )
    await ui.button(actor, "Повторить сохранённый запрос")
    await ui.wait(
        actor,
        "sessionStorage.getItem('detective.pendingAction.v1:'+location.pathname.split('/')[2])===null",
    )
    assert await ui.revision(actor) == before + 1
    # Finish the remaining cards by real controls, not direct API calls.
    while await ui.evaluate(
        host, "document.querySelector('[data-phase]').dataset.phase==='PLAYING'"
    ):
        actor = await ui.active(host, guest)
        revision = await ui.revision(actor)
        await ui.commit_card(actor, "discard")
        for session in (host, guest):
            await ui.wait_revision(session, revision + 1)
    await ui.ready(host, "DISCUSSION")
    await ui.click(guest, "[data-testid=settings]")
    await ui.button(guest, "Предложить завершить расследование")
    await ui.button(guest, "Предложить завершить расследование")
    await ui.click(host, "[data-testid=settings]")
    await ui.button(host, "Предложить завершить расследование")
    await ui.button(host, "Перейти к вопросам")
    await ui.ready(host, "ANSWERING")
    for index in range(1, 5):
        await ui.ready(guest, "ANSWERING")
        await ui.click(guest, f"[data-question=q{index}] input[value=a]")
        await ui.wait(
            host,
            f'document.querySelector("[data-question=q{index}] input[value=a]")?.checked',
        )
    await ui.ready(host, "ANSWERING")
    await ui.button(host, "Отправить ответы команды")
    await ui.button(host, "Зафиксировать ответы")
    await ui.ready(host, "REVIEW")
    await ui.ready(guest, "REVIEW")
    assert await ui.evaluate(
        guest,
        "document.querySelectorAll('.review-key').length===0 && "
        "Array.from(document.querySelectorAll('input[type=radio]')).every(i=>i.disabled)",
    )
    await ui.click(host, "[data-question=q1] .review-actions button:first-child")
    await ui.ready(host, "REVIEW")
    await ui.call("Page.reload", session=host)
    await ui.wait(host, "!!document.querySelector('[data-phase]')")
    await ui.ready(host, "REVIEW")
    assert await ui.evaluate(
        host,
        'document.querySelector("[data-question=q1] .review-actions '
        'button:first-child").getAttribute("aria-pressed")==="true"',
    )
    for index in range(2, 5):
        await ui.ready(host)
        revision = await ui.revision(host)
        try:
            await ui.click(host, f"[data-question=q{index}] .review-actions button:nth-child(2)")
        except RuntimeError:
            summary = await ui.evaluate(
                host,
                """(()=>({
              phase:document.querySelector('[data-phase]')?.dataset.phase,
              revision:document.querySelector('[data-phase]')?.dataset.revision,
              pending:!!sessionStorage.getItem('detective.pendingAction.v1:'+location.pathname.split('/')[2]),
              status:document.querySelector('[data-testid=connection-status]')?.textContent,
              questions:document.querySelectorAll('[data-question]').length,
              error:document.querySelector('[role=alert]')?.textContent
            }))()""",
            )
            raise RuntimeError(f"Review control q{index} unavailable: {summary}") from None
        # CDP may return from mouseReleased before React renders busy/disabled.
        # Wait for the acknowledged server change, not the previous idle DOM.
        await ui.wait_revision(host, revision + 1)
    await ui.ready(host, "RESULTS")
    await ui.ready(guest, "RESULTS")
    assert await ui.evaluate(
        host,
        "['0','1','2'].includes(document.querySelector('[data-testid=score]').textContent)",
    )
    revision = await ui.revision(host)
    await ui.button(host, "Завершить партию")
    await ui.wait_revision(host, revision + 1)
    for session in (host, guest):
        await ui.ready(session, "FINISHED")
        assert await ui.evaluate(
            session,
            "document.querySelector('[data-testid=score]')!==null && "
            "!document.querySelector('[data-testid=private-card]')",
        )


async def test_full_react_game_network_recovery_and_finished(pg, frontend, live, tmp_path):
    executable = os.environ.get("DETECTIVE_CHROME_BIN")
    if not executable:
        pytest.skip("Set DETECTIVE_CHROME_BIN for React browser E2E")
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    process = subprocess.Popen(
        [
            executable,
            "--headless=new",
            f"--remote-debugging-port={port}",
            "--remote-debugging-address=127.0.0.1",
            f"--user-data-dir={tmp_path / 'ui-chrome'}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-background-networking",
            "--disable-sync",
            "--disable-component-update",
            "--disable-extensions",
            "about:blank",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        async with httpx.AsyncClient() as probe:
            for _ in range(150):
                try:
                    response = await probe.get(f"http://127.0.0.1:{port}/json/version")
                    if response.status_code == 200:
                        uri = response.json()["webSocketDebuggerUrl"]
                        break
                except httpx.TransportError:
                    pass
                await asyncio.sleep(0.05)
            else:
                raise RuntimeError("Test Chrome did not start")
        async with connect(uri, proxy=None, max_size=2**22) as channel:
            ui = UI(channel)

            async def restart():
                await live.stop(kill=True)
                await live.launch()

            await full_game_scenario(ui, frontend, restart, tmp_path)
    finally:
        if process.poll() is None:
            process.terminate()
            await asyncio.to_thread(process.wait, timeout=5)


async def test_ui_deleted_and_expired_session_clear_private_cards(pg, frontend, tmp_path):
    executable = os.environ.get("DETECTIVE_CHROME_BIN")
    if not executable:
        pytest.skip("Set DETECTIVE_CHROME_BIN for React authentication E2E")
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    process = subprocess.Popen(
        [
            executable,
            "--headless=new",
            f"--remote-debugging-port={port}",
            "--remote-debugging-address=127.0.0.1",
            f"--user-data-dir={tmp_path / 'auth-chrome'}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-background-networking",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        async with httpx.AsyncClient() as probe:
            for _ in range(100):
                try:
                    r = await probe.get(f"http://127.0.0.1:{port}/json/version")
                    if r.status_code == 200:
                        uri = r.json()["webSocketDebuggerUrl"]
                        break
                except httpx.TransportError:
                    pass
                await asyncio.sleep(0.05)
            else:
                raise RuntimeError("Chrome did not become ready")
        async with connect(uri, proxy=None, max_size=2**22) as channel:
            ui = UI(channel)
            host, context = await ui.page_ui(frontend)
            await ui.fill(host, "#nickname", "Ведущий")
            await ui.click(host, "[data-testid=create-room]")
            await ui.ready(host)
            code = await ui.evaluate(host, "location.pathname.split('/')[2]")
            guest, _ = await ui.page_ui(frontend + "/rooms/" + code)
            await ui.fill(guest, "#nickname", "Участник")
            await ui.click(guest, "[data-testid=join-room]")
            await ui.ready(guest)
            await ui.wait(host, "document.querySelectorAll('[data-member-id]').length===2")
            await ui.evaluate(
                host,
                "document.querySelector('#scenario').value='envelope-demo'; "
                "document.querySelector('#scenario').dispatchEvent(new "
                "Event('change',{bubbles:true})); true",
            )
            await ui.ready(host)
            await ui.click(host, "[data-testid=start-game]")
            await ui.ready(host, "INTRO")
            await ui.button(host, "Начать игру")
            await ui.ready(guest, "PLAYING")
            await ui.call(
                "Emulation.setDeviceMetricsOverride",
                {"width": 390, "height": 844, "deviceScaleFactor": 1, "mobile": True},
                guest,
            )
            assert await ui.evaluate(
                guest, "document.documentElement.scrollWidth<=window.innerWidth"
            )
            assert await ui.evaluate(
                guest, "document.querySelectorAll('[data-testid=private-card]').length===3"
            )
            await ui.call("Storage.clearCookies", {"browserContextId": context})
            await ui.call("Page.reload", session=host)
            await ui.wait(
                host,
                "!!document.querySelector('#nickname') && "
                "!document.querySelector('#nickname').disabled",
            )
            assert await ui.evaluate(
                host,
                "!document.querySelector('[data-testid=private-card]') && "
                "!document.querySelector('[data-phase]')",
            )
            from datetime import UTC, datetime, timedelta

            from detective.api.db import AnonymousSession, Member
            from sqlalchemy import select

            async with pg.app.state.factory.begin() as db:
                session = await db.scalar(
                    select(AnonymousSession)
                    .join(Member, Member.session_id == AnonymousSession.id)
                    .where(Member.nickname == "Участник")
                )
                session.expires_at = datetime.now(UTC) - timedelta(seconds=1)
            await ui.wait(
                guest,
                "!!document.querySelector('#nickname') && "
                "document.querySelector('#nickname').disabled",
            )
            assert await ui.evaluate(
                guest,
                "!document.querySelector('[data-testid=private-card]') && "
                "!document.querySelector('[data-phase]')",
            )
    finally:
        if process.poll() is None:
            process.terminate()
            await asyncio.to_thread(process.wait, timeout=5)
