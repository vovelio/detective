"""Real HTTPS/WSS + full party and destructive recovery, only in owned test projects.

Run explicitly with DETECTIVE_DOCKER_TESTS=1 and DETECTIVE_CHROME_BIN. No public
resources, DNS changes, global certificate trust, existing Compose projects or local
PostgreSQL instances are accessed. Secret responses are never attached to failures.
"""

import asyncio
import base64
import hashlib
import ipaddress
import json
import os
import secrets
import shutil
import socket
import ssl
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest
from integration.test_ui import UI, full_game_scenario
from websockets.asyncio.client import connect

ROOT = Path(__file__).resolve().parents[2]
pytestmark = pytest.mark.asyncio


def free_port():
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def command(args, *, timeout=600):
    result = subprocess.run(args, cwd=ROOT, capture_output=True, timeout=timeout)
    if result.returncode:
        # Docker/third-party output is not guaranteed to redact secrets.
        raise RuntimeError("Isolated deployment command failed; output intentionally withheld")
    return result.stdout


@dataclass
class Stack:
    env: Path
    project: str
    http_port: int
    https_port: int
    subnet: str

    @property
    def origin(self):
        return f"https://localhost:{self.https_port}"

    @property
    def compose(self):
        return [
            "docker",
            "compose",
            "--env-file",
            str(self.env),
            "-p",
            self.project,
            "-f",
            str(ROOT / "compose.yaml"),
            "-f",
            str(ROOT / "compose.test.yaml"),
        ]

    async def ops(self, *args):
        return await asyncio.to_thread(
            command,
            [
                sys.executable,
                str(ROOT / "infra/ops.py"),
                "--env-file",
                str(self.env),
                "--project",
                self.project,
                "--test",
                *map(str, args),
            ],
        )

    async def initialize(self):
        await self.ops(
            "init",
            "--domain",
            "localhost",
            "--https-port",
            self.https_port,
            "--http-port",
            self.http_port,
            "--subnet",
            self.subnet,
        )

    async def run_compose(self, *args):
        return await asyncio.to_thread(command, [*self.compose, *map(str, args)])

    async def ca(self, target):
        await self.run_compose("cp", "proxy:/data/caddy/pki/authorities/local/root.crt", target)
        target.chmod(0o600)
        return ssl.create_default_context(cafile=str(target))

    async def wait_ready(self, context):
        async with httpx.AsyncClient(verify=context, trust_env=False) as client:
            for _ in range(300):
                try:
                    reply = await client.get(self.origin + "/health/ready", timeout=3)
                    if reply.status_code == 200:
                        return
                except httpx.TransportError:
                    pass
                await asyncio.sleep(0.1)
        raise RuntimeError("Isolated HTTPS deployment did not become ready")

    async def assert_private_ports(self):
        config = json.loads(await self.run_compose("config", "--format", "json"))
        assert not config["services"]["db"].get("ports")
        assert not config["services"]["api"].get("ports")
        bindings = config["services"]["proxy"]["ports"]
        assert bindings and all(p.get("host_ip") == "127.0.0.1" for p in bindings)
        proxy_ip = config["services"]["proxy"]["networks"]["application"]["ipv4_address"]
        pools = config["networks"]["application"]["ipam"]["config"]
        assert all("ip_range" in pool for pool in pools)
        assert all(
            ipaddress.ip_address(proxy_ip) not in ipaddress.ip_network(pool["ip_range"])
            for pool in pools
        )

    async def assert_database_role(self):
        script = """import asyncio,json
from sqlalchemy import text
from detective.api.config import Settings
from detective.api.db import make_engine
async def check():
    engine=make_engine(Settings.from_env())
    try:
        async with engine.connect() as db:
            query="SELECT current_user,rolsuper,rolcreatedb,rolcreaterole "
            query+="FROM pg_roles WHERE rolname=current_user"
            row=(await db.execute(text(query))).one()
            print(json.dumps(list(row)))
    finally:
        await engine.dispose()
asyncio.run(check())
"""
        result = json.loads(await self.run_compose("exec", "-T", "api", "python", "-c", script))
        assert result == ["detective_app", False, False, False]

    async def assert_owned_volumes(self):
        ids = (
            (
                await asyncio.to_thread(
                    command,
                    [
                        "docker",
                        "volume",
                        "ls",
                        "-q",
                        "--filter",
                        f"label=com.docker.compose.project={self.project}",
                    ],
                )
            )
            .decode()
            .splitlines()
        )
        assert ids
        volumes = json.loads(
            await asyncio.to_thread(command, ["docker", "volume", "inspect", *ids])
        )
        assert all(
            v.get("Labels", {}).get("com.docker.compose.project") == self.project for v in volumes
        )
        return ids

    async def destroy(self):
        if not self.project.startswith("detective-test-"):
            raise RuntimeError("Refusing to destroy a non-test project")
        await self.ops("destroy-test")


def spki(context, port):
    with socket.create_connection(("127.0.0.1", port), timeout=5) as connection:
        with context.wrap_socket(connection, server_hostname="localhost") as tls:
            certificate = tls.getpeercert(binary_form=True)
    public = subprocess.run(
        ["openssl", "x509", "-inform", "DER", "-pubkey", "-noout"],
        input=certificate,
        capture_output=True,
        check=True,
    ).stdout
    der = subprocess.run(
        ["openssl", "pkey", "-pubin", "-outform", "DER"],
        input=public,
        capture_output=True,
        check=True,
    ).stdout
    return base64.b64encode(hashlib.sha256(der).digest()).decode()


class DockerUI(UI):
    async def wait(self, session, expression):
        # Container restart/restore can outlast the short development polling loop.
        for _ in range(900):
            try:
                if await self.evaluate(session, expression):
                    return
            except RuntimeError:
                pass
            await asyncio.sleep(0.05)
        # Never include the predicate, page text, cookie or private data in errors.
        # These public counters make an intermittent transition failure actionable.
        diagnostic = await self.evaluate(
            session,
            """(()=>({
              phase:document.querySelector('[data-phase]')?.dataset.phase,
              revision:document.querySelector('[data-phase]')?.dataset.revision,
              busy:document.querySelector('.app-shell')?.getAttribute('aria-busy'),
              connected:document.querySelector('[data-testid=connection-status]')
                ?.textContent==='На связи',
              pending:!!sessionStorage.getItem(
                'detective.pendingAction.v1:'+location.pathname.split('/')[2]),
              reviewMarks:document.querySelectorAll('.review-actions [aria-pressed=true]').length,
              alert:!!document.querySelector('[role=alert]'),
              rateLimited:document.querySelector('[role=alert]')?.textContent
                ?.includes('Слишком много запросов')??false,
              revisionConflict:document.querySelector('[role=alert]')?.textContent
                ?.includes('Состояние изменилось.')===true
            }))()""",
        )
        raise RuntimeError(f"Isolated browser condition timed out: {diagnostic}")


async def test_isolated_compose_https_full_game_and_backup_restore(tmp_path):
    if os.environ.get("DETECTIVE_DOCKER_TESTS") != "1":
        pytest.skip("Set DETECTIVE_DOCKER_TESTS=1 for isolated destructive Compose acceptance")
    executable = os.environ.get("DETECTIVE_CHROME_BIN")
    if not executable:
        pytest.fail("DETECTIVE_CHROME_BIN is required for opted-in Compose acceptance")
    if not shutil.which("docker"):
        pytest.fail("Docker is unavailable; opted-in Compose acceptance cannot pass")
    await asyncio.to_thread(command, ["docker", "info", "--format", "{{.ServerVersion}}"])
    token = secrets.token_hex(5)
    http_port, https_port = free_port(), free_port()
    stacks = [
        Stack(
            tmp_path / "first.env",
            f"detective-test-{token}-a",
            http_port,
            https_port,
            f"172.29.{secrets.randbelow(200) + 20}.0/24",
        )
    ]
    first = stacks[0]
    process = None
    try:
        await first.initialize()
        await first.ops("start")
        await first.assert_private_ports()
        await first.assert_database_role()
        await first.assert_owned_volumes()
        context = await first.ca(tmp_path / "root-ca.crt")
        await first.wait_ready(context)
        async with httpx.AsyncClient(verify=context, trust_env=False) as client:
            response = await client.get(first.origin)
            assert response.status_code == 200
            assert b"/src/main.tsx" not in response.content  # Built React, never Vite.
            redirect = await client.get(f"http://localhost:{http_port}/", follow_redirects=False)
            assert redirect.status_code in (301, 302, 307, 308)
            assert redirect.headers["location"].startswith("https://")
            denied = await client.post(first.origin + "/api/v1/sessions", json={})
            assert denied.status_code == 403
            session = await client.post(
                first.origin + "/api/v1/sessions", json={}, headers={"Origin": first.origin}
            )
            assert session.status_code == 200
            cookie = session.headers.get("set-cookie", "")
            # Keep raw session values out of pytest assertion explanations.
            valid_cookie = (
                cookie.startswith("__Host-detective_session=")
                and all(flag in cookie.lower() for flag in ("httponly", "secure", "samesite=lax"))
                and "domain=" not in cookie.lower()
                and "path=/" in cookie.lower()
            )
            assert valid_cookie

        pin = await asyncio.to_thread(spki, context, https_port)
        port = free_port()
        process = subprocess.Popen(
            [
                executable,
                "--headless=new",
                f"--remote-debugging-port={port}",
                "--remote-debugging-address=127.0.0.1",
                f"--user-data-dir={tmp_path / 'chrome'}",
                f"--ignore-certificate-errors-spki-list={pin}",
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
        async with httpx.AsyncClient(trust_env=False) as probe:
            for _ in range(200):
                try:
                    result = await probe.get(f"http://127.0.0.1:{port}/json/version")
                    if result.status_code == 200:
                        uri = result.json()["webSocketDebuggerUrl"]
                        break
                except httpx.TransportError:
                    pass
                await asyncio.sleep(0.05)
            else:
                raise RuntimeError("Isolated test Chrome did not start")
        active = first

        async def restart():
            await active.ops("restart-api")
            await active.wait_ready(context)

        async def checkpoint(ui, host, guest, code):
            nonlocal active
            # Only hashes and public object metadata leave the browser.
            hands = [await ui.fingerprint(p) for p in (host, guest)]
            before = await ui.evaluate(
                host,
                f"fetch('/api/v1/rooms/{code}/snapshot').then(r=>r.json()).then(s=>"
                "({revision:s.revision,board:s.board,phase:s.phase,host_id:s.host_id}))",
            )
            for page in (host, guest):
                cookies = await ui.call("Network.getCookies", {"urls": [active.origin]}, page)
                secure = [c for c in cookies["cookies"] if c["name"] == "__Host-detective_session"]
                valid_cookie = len(secure) == 1 and all(
                    c["secure"] and c["httpOnly"] for c in secure
                )
                assert valid_cookie
            # UI connection status can become connected only after actual WSS hello/resume.
            assert await ui.evaluate(
                host,
                "location.protocol==='https:' && "
                "document.querySelector('[data-testid=connection-status]')"
                ".textContent==='На связи'",
            )
            backup = tmp_path / "backup"
            await active.ops("backup", backup)
            old = active
            old_ids = await old.assert_owned_volumes()
            await old.destroy()
            remaining = (
                (
                    await asyncio.to_thread(
                        command,
                        [
                            "docker",
                            "volume",
                            "ls",
                            "-q",
                            "--filter",
                            f"label=com.docker.compose.project={old.project}",
                        ],
                    )
                )
                .decode()
                .splitlines()
            )
            assert not remaining
            assert old_ids
            restored = Stack(
                tmp_path / "restored.env",
                f"detective-test-{token}-b",
                http_port,
                https_port,
                old.subnet,
            )
            stacks.append(restored)
            await restored.initialize()
            await restored.ops("restore", backup)
            active = restored
            await active.wait_ready(context)
            await active.assert_owned_volumes()
            await active.assert_database_role()
            for index, page in enumerate((host, guest)):
                await ui.call("Page.reload", session=page)
                await ui.wait(page, "!!document.querySelector('[data-phase]')")
                await ui.ready(page, "PLAYING")
                matches = await ui.fingerprint(page) == hands[index]
                assert matches
                await ui.wait(
                    page,
                    "Array.from(document.querySelectorAll('[data-testid=private-card] "
                    "img')).every(i=>i.complete&&i.naturalWidth>0)",
                )
            after = await ui.evaluate(
                host,
                f"fetch('/api/v1/rooms/{code}/snapshot').then(r=>r.json()).then(s=>"
                "({revision:s.revision,board:s.board,phase:s.phase,host_id:s.host_id}))",
            )
            assert after == before

        async with connect(uri, proxy=None, max_size=2**22) as channel:
            await full_game_scenario(DockerUI(channel), first.origin, restart, tmp_path, checkpoint)
        # Prove the production middleware limits requests through the real proxy;
        # client-controlled forwarding headers cannot manufacture fresh identities.
        async with httpx.AsyncClient(verify=context, trust_env=False) as client:
            limited = None
            for attempt in range(40):
                response = await client.post(
                    active.origin + "/api/v1/sessions",
                    json={},
                    headers={
                        "Origin": active.origin,
                        "X-Forwarded-For": f"203.0.113.{attempt + 1}",
                    },
                )
                if response.status_code == 429:
                    limited = response
                    break
                assert response.status_code == 200
            assert limited is not None
            assert limited.json()["error"]["code"] == "RATE_LIMITED"
            assert int(limited.headers["retry-after"]) > 0
    finally:
        if process is not None and process.poll() is None:
            process.terminate()
            await asyncio.to_thread(process.wait, timeout=5)
        for stack in reversed(stacks):
            if stack.env.exists():
                await stack.destroy()
