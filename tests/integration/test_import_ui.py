"""Independent HTTP-import acceptance using real Chrome file selection and PostgreSQL."""

import pytest

from .test_board_ui import browser_ui  # noqa: F401
from .test_ui import frontend, make_upload_package  # noqa: F401

pytestmark = pytest.mark.asyncio


async def test_import_errors_permissions_retry_and_room_isolation(
    pg,
    frontend,  # noqa: F811 — imported pytest fixture
    live,
    browser_ui,  # noqa: F811 — imported pytest fixture
    tmp_path,
):
    ui = browser_ui
    host, _ = await ui.page_ui(frontend)
    await ui.fill(host, "#nickname", "Ведущий")
    await ui.click(host, "[data-testid=create-room]")
    try:
        await ui.ready(host)
    except RuntimeError:
        diagnostic = await ui.evaluate(
            host,
            "({path:location.pathname,status:document.querySelector('[data-testid=connection-status]')?.textContent,error:document.querySelector('[role=alert]')?.textContent})",
        )
        raise RuntimeError(f"Lobby did not become ready: {diagnostic}") from None
    code = await ui.evaluate(host, "location.pathname.split('/')[2]")
    guest, _ = await ui.page_ui(frontend + "/rooms/" + code)
    await ui.fill(guest, "#nickname", "Участник")
    await ui.click(guest, "[data-testid=join-room]")
    await ui.ready(guest)
    assert await ui.evaluate(
        guest,
        "!document.querySelector('[data-testid=import-open]') || "
        "document.querySelector('[data-testid=import-open]').disabled",
    )

    invalid = tmp_path / "invalid.detective"
    invalid.write_bytes(b"PRIVATE_SENTINEL_SECRET invalid archive")
    await ui.click(host, "[data-testid=import-open]")
    await ui.upload_file(host, invalid)
    await ui.click(host, "[data-testid=import-submit]")
    await ui.wait_import(host, "error")
    assert await ui.evaluate(
        host,
        "!document.querySelector('[data-testid=import-form]').textContent"
        ".includes('PRIVATE_SENTINEL_SECRET')",
    )
    assert await ui.evaluate(
        host, "document.querySelectorAll('[data-testid=import-issues] li').length>0"
    )

    package = make_upload_package(tmp_path, source_id="browser-import-retry")
    await ui.upload_file(host, package)
    # Lose only the HTTP response after the real upload transaction has committed.
    await ui.call(
        "Fetch.enable",
        {"patterns": [{"urlPattern": "*/cases/import", "requestStage": "Response"}]},
        host,
    )
    await ui.click(host, "[data-testid=import-submit]")
    paused = await ui.event("Fetch.requestPaused")
    await ui.call(
        "Fetch.failRequest",
        {"requestId": paused["requestId"], "errorReason": "ConnectionReset"},
        host,
    )
    await ui.call("Fetch.disable", session=host)
    await ui.wait_import(host, "uncertain")
    await ui.click(host, "[data-testid=import-refresh]")
    await ui.wait(
        host,
        "Array.from(document.querySelector('#scenario').options).filter("
        "o=>o.textContent.includes('Загруженное учебное дело')).length===1",
    )
    # Retry the exact same bytes. It must remain one room-local entry, not a new scenario.
    await ui.click(host, "[data-testid=import-submit]")
    await ui.wait_import(host, "success")
    await ui.click(host, "[data-testid=import-close]")
    assert await ui.evaluate(
        host,
        "Array.from(document.querySelector('#scenario').options).filter("
        "o=>o.textContent.includes('Загруженное учебное дело')).length===1",
    )
    selected = await ui.evaluate(host, "document.querySelector('#scenario').value")
    assert not selected  # Upload must not silently select a case.

    # Another room and the global builtin catalog never acquire the uploaded case.
    other, _ = await ui.page_ui(frontend)
    await ui.fill(other, "#nickname", "ДругаяКомната")
    await ui.click(other, "[data-testid=create-room]")
    await ui.ready(other)
    assert await ui.evaluate(
        other,
        "!Array.from(document.querySelector('#scenario').options).some("
        "o=>o.textContent.includes('Загруженное учебное дело'))",
    )
    assert await ui.evaluate(
        host,
        "fetch('/api/v1/cases').then(r=>r.json()).then(items=>"
        "!items.some(c=>c.meta.title==='Загруженное учебное дело'))",
    )
    await live.stop(kill=True)
    await live.launch()
    await ui.call("Page.reload", session=host)
    await ui.wait(host, "!!document.querySelector('#scenario')")
    await ui.ready(host)
    assert await ui.evaluate(
        host,
        "Array.from(document.querySelector('#scenario').options).filter("
        "o=>o.textContent.includes('Загруженное учебное дело')).length===1",
    )
    assert await ui.evaluate(
        host,
        f"fetch('/api/v1/rooms/{code}/cases').then(r=>r.json()).then(items=>"
        "items.filter(c=>c.meta.title==='Загруженное учебное дело').length===1)",
    )
    # Only opaque catalog IDs and safe metadata are exposed, never the source manifest.
    assert await ui.evaluate(
        guest,
        f"fetch('/api/v1/rooms/{code}/cases').then(r=>r.json()).then(items=>"
        "!JSON.stringify(items).includes('correct_option_id') && "
        "!JSON.stringify(items).includes('logic_notes') && "
        "!JSON.stringify(items).includes('full_story'))",
    )
