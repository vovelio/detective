"""Two isolated real Chrome contexts, HttpOnly cookies, WS notifications and page reload.

Opt in via DETECTIVE_CHROME_BIN. CDP needs no Playwright/Selenium dependency.
Only boolean checks and non-secret hashes are returned from browser evaluations.
"""

import asyncio
import json
import os
import socket
import subprocess

import httpx
import pytest
from websockets.asyncio.client import connect

pytestmark = pytest.mark.asyncio


class CDP:
    def __init__(self, socket):
        self.socket, self.counter = socket, 0
        self.events = []

    async def call(self, method, params=None, session=None):
        self.counter += 1
        request = {"id": self.counter, "method": method, "params": params or {}}
        if session:
            request["sessionId"] = session
        await self.socket.send(json.dumps(request))
        while True:
            reply = json.loads(await asyncio.wait_for(self.socket.recv(), 5))
            if "method" in reply:
                self.events.append(reply)
            if reply.get("id") == self.counter:
                if "error" in reply:
                    raise RuntimeError("Browser protocol error")
                return reply.get("result", {})

    async def evaluate(self, session, expression):
        reply = await self.call(
            "Runtime.evaluate",
            {"expression": expression, "awaitPromise": True, "returnByValue": True},
            session,
        )
        if "exceptionDetails" in reply:
            raise RuntimeError("Browser script failed (details intentionally not logged)")
        return reply["result"].get("value")

    async def wait(self, session, expression):
        for _ in range(100):
            try:
                if await self.evaluate(session, expression):
                    return
            except RuntimeError:
                pass  # execution context can be replaced during reload/navigation
            await asyncio.sleep(0.03)
        raise RuntimeError("Browser condition did not become true")

    async def page(self, origin):
        context = (await self.call("Target.createBrowserContext"))["browserContextId"]
        target = (
            await self.call(
                "Target.createTarget",
                {
                    "url": origin + "/health/live",
                    "browserContextId": context,
                },
            )
        )["targetId"]
        session = (await self.call("Target.attachToTarget", {"targetId": target, "flatten": True}))[
            "sessionId"
        ]
        await self.wait(session, "location.origin === " + json.dumps(origin))
        await self.evaluate(
            session,
            """(async () => {
            window.post = async (path, body) => {
                const headers={'Content-Type':'application/json',
                    ...(window.csrf ? {'X-CSRF-Token':window.csrf} : {})};
                const r = await fetch(path, {method:'POST', headers,
                    body:JSON.stringify(body)});
                if (!r.ok) throw Error('API request failed');
                return await r.json();
            };
            window.csrf = (await post('/api/v1/sessions', {})).csrf_token;
            return true;
        })()""",
        )
        return session

    async def subscribe(self, session, code):
        await self.evaluate(
            session,
            """(async () => {
            window.events = [];
            window.socket = new WebSocket(location.origin.replace('http','ws') +
                '/ws/v1/rooms/' + CODE, 'detective.v1');
            socket.onmessage = e => {
                const m = JSON.parse(e.data);
                if (m.type === 'ping') {
                    socket.send(JSON.stringify({type:'pong',protocol_version:1}));
                }
                events.push({type:m.type,revision:m.revision,phase:m.phase});
            };
            await new Promise((resolve,reject) => {socket.onopen=resolve; socket.onerror=reject;});
            return true;
        })()""".replace("CODE", json.dumps(code)),
        )
        await self.wait(session, "events.some(e => e.type === 'hello')")


async def test_two_browser_contexts_sync_privacy_and_reload(pg, live, tmp_path):
    executable = os.environ.get("DETECTIVE_CHROME_BIN")
    if not executable:
        pytest.skip("Set DETECTIVE_CHROME_BIN to enable real two-context Chrome test")
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    process = subprocess.Popen(
        [
            executable,
            "--headless=new",
            f"--remote-debugging-port={port}",
            "--remote-debugging-address=127.0.0.1",
            f"--user-data-dir={tmp_path / 'chrome'}",
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
        browser_uri = None
        async with httpx.AsyncClient() as probe:
            for _ in range(100):
                if process.poll() is not None:
                    raise RuntimeError("Test browser exited")
                try:
                    reply = await probe.get(f"http://127.0.0.1:{port}/json/version")
                    if reply.status_code == 200:
                        browser_uri = reply.json()["webSocketDebuggerUrl"]
                        break
                except httpx.TransportError:
                    pass
                await asyncio.sleep(0.05)
        assert browser_uri is not None
        async with connect(browser_uri, proxy=None, max_size=2**20) as channel:
            cdp = CDP(channel)
            host = await cdp.page(live.origin)
            guest = await cdp.page(live.origin)
            public_info = await cdp.evaluate(
                host,
                """(async () => {
                const r = await post('/api/v1/rooms', {nickname:'Ведущий'});
                window.roomCode=r.code; window.memberId=r.member_id;
                return {code:r.code, member:r.member_id};
            })()""",
            )
            code = public_info["code"]
            await cdp.evaluate(
                guest,
                """(async () => {
                const r = await post('/api/v1/rooms/' + CODE + '/join', {nickname:'Участник'});
                window.memberId=r.member_id; return true;
            })()""".replace("CODE", json.dumps(code)),
            )
            await cdp.subscribe(host, code)
            await cdp.subscribe(guest, code)
            await cdp.evaluate(
                host,
                """(async () => {
                const p='/api/v1/rooms/' + roomCode;
                await post(p+'/cases/select',{case_id:'envelope-demo'});
                await post(p+'/start',{client_action_id:crypto.randomUUID(),expected_revision:0});
                await post(p+'/actions',{client_action_id:crypto.randomUUID(),expected_revision:1,
                    type:'begin_play',payload:{}});
                return true;
            })()""",
            )
            for session in (host, guest):
                await cdp.wait(
                    session, "events.some(e => e.type === 'state_changed' && e.revision === 2)"
                )
                assert await cdp.evaluate(session, "!document.cookie.includes('detective_session')")
            # Keep private data in each browser; return only hashes and public metadata.
            inspect_script = """(async () => {
                window.view=await (await fetch('/api/v1/rooms/'+CODE+'/snapshot')).json();
                const raw=new TextEncoder().encode(JSON.stringify(view.private.hand));
                const hash=Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',raw)))
                    .map(v=>v.toString(16).padStart(2,'0')).join('');
                const forbidden=(v)=>v && typeof v==='object' && (Object.keys(v).some(k=>
                    ['role','logic_notes','supports_questions','solution','correct_option_id',
                     'manifest_json'].includes(k)) || Object.values(v).some(forbidden));
                return {hash,clean:!forbidden(view),revision:view.revision,
                    active:view.active_player_id,
                    host:view.host_id,count:view.private.hand.length,member:window.memberId};
            })()""".replace("CODE", json.dumps(code))
            a, b = (
                await cdp.evaluate(host, inspect_script),
                await cdp.evaluate(guest, inspect_script),
            )
            assert a["clean"] and b["clean"] and a["hash"] != b["hash"]
            assert a["count"] == b["count"] == 3
            actor = host if a["active"] == a["member"] else guest
            await cdp.evaluate(
                actor,
                """(async () => {
                await post('/api/v1/rooms/'+CODE+'/actions',{
                    client_action_id:crypto.randomUUID(),expected_revision:view.revision,
                    type:'play_card',payload:{card_id:view.private.hand[0].id,mode:'discard',confirmed:true}});
                return true;
            })()""".replace("CODE", json.dumps(code)),
            )
            for session in (host, guest):
                await cdp.wait(
                    session, "events.some(e=>e.type==='state_changed' && e.revision===3)"
                )
            saved = await cdp.evaluate(host, inspect_script)
            await cdp.call("Page.reload", session=host)
            await cdp.wait(host, "typeof window.socket === 'undefined'")
            # Bootstrap after a real page reload reuses the browser's HttpOnly cookie.
            await cdp.evaluate(
                host,
                """(async () => {
                const r=await fetch('/api/v1/sessions',{method:'POST',
                    headers:{'Content-Type':'application/json'},body:'{}'});
                const s=await r.json(); window.memberId=s.rooms[0].member_id; return r.ok;
            })()""",
            )
            restored = await cdp.evaluate(host, inspect_script)
            assert restored == saved
            await cdp.subscribe(host, code)
            await cdp.wait(host, "events.some(e=>e.type==='hello' && e.revision===3)")
    finally:
        if process.poll() is None:
            process.terminate()
            await asyncio.to_thread(process.wait, timeout=5)
