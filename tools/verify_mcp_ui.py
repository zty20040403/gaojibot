"""Exercise the built console against fake MCP APIs; never contact a bot/server."""

from __future__ import annotations

import argparse
import asyncio
from copy import deepcopy
import json
from pathlib import Path

from playwright.async_api import async_playwright


async def verify(browser, root, output, width, height):
    context = await browser.new_context(viewport={"width": width, "height": height})
    page = await context.new_page()
    errors, writes = [], []
    page.on("pageerror", lambda error: errors.append(str(error)))
    server = {"name": "example", "config": {"transport": "http", "url": "https://example.invalid/mcp", "credential_ref": ""},
              "scopes": ["onebot-v11:group:100"], "enabled": False, "version": 1, "updated_at": 1793090000}
    state = {"version": 1, "sequence": 1, "changed": False, "configured": True, "servers": [server]}
    tools = [{"name": f"mcp__example__lookup_{i}", "remote_name": f"lookup_{i}", "server": "example",
              "present": True, "enabled": False, "trust": "approval", "version": 1, "fingerprint": "a" * 64,
              "definition": {"name": f"lookup_{i}", "description": "外部工具说明，不作为宿主指令。" * 40,
                             "inputSchema": {"type": "object", "properties": {"query": {"type": "string"}}}}} for i in range(7)]
    tokens = [{"token_id": f"token-{i}", "label": f"测试客户端 {i}", "scope_key": "onebot-v11:group:100",
               "bot_id": "200", "version": 1, "revoked": False, "expires_at": 4000000000, "created_at": 1793090000} for i in range(7)]
    calls = [{"call_id": f"call-{i}", "server_name": "example", "remote_name": f"lookup_{i}",
              "status": "needs_attention" if i == 0 else "succeeded", "updated_at": 1793090000,
              "result": {"ok": i != 0, "text": "<script>window.unsafe = true</script>"}} for i in range(9)]

    async def route_handler(route):
        request = route.request
        path = request.url.split("mcp-admin.test", 1)[-1]
        if path.startswith("/bot-admin/api/v1"):
            path = path.removeprefix("/bot-admin/api/v1")
            payload = {}
            if path == "/me":
                payload = {"account": {"username": "fixture", "role": "admin"}}
            elif path == "/events":
                state["sequence"] += 1
                event = {"type": "resources.changed" if state["changed"] else "ready", "sequence": state["sequence"],
                         "resources": ["mcp"], "versions": {"mcp": state["version"]}}
                state["changed"] = False
                await route.fulfill(content_type="text/event-stream", body="data: " + json.dumps(event) + "\n\n")
                return
            elif request.method in {"PUT", "POST"}:
                body = request.post_data_json
                assert request.headers.get("if-match") == f'"{state["version"]}"'
                writes.append((path, body))
                state["version"] += 1
                payload = {"resource": "mcp", "resource_version": state["version"]}
                if path.endswith("/refresh"):
                    current = next(item for item in state["servers"] if item["name"] == path.split("/")[3])
                    assert body == {"expected_version": current["version"]}
                    current["version"] += 1
                    payload.update(server=deepcopy(current), tools=deepcopy(tools))
                elif "/tools/" in path:
                    tool = next(item for item in tools if item["remote_name"] == path.rsplit("/", 1)[-1])
                    assert body["expected_version"] == tool["version"] and body["tool_fingerprint"] == tool["fingerprint"]
                    assert body["server_version"] == server["version"]
                    tool.update(enabled=body["enabled"], trust=body["trust"], version=tool["version"] + 1)
                    payload["tool"] = deepcopy(tool)
                elif path.startswith("/mcp/servers/"):
                    assert body["config"]["transport"] == "http"
                    assert set(body["config"]) <= {"transport", "url", "credential_ref"} and body["config"]["url"]
                    name = path.rsplit("/", 1)[-1]
                    current = next((item for item in state["servers"] if item["name"] == name), None)
                    assert body["expected_version"] == (current["version"] if current else 0)
                    if current is None:
                        current = {"name": name, "version": 0}
                        state["servers"].append(current)
                    current.update(config=body["config"], scopes=body["scopes"], enabled=body["enabled"] if current["version"] else False, version=current["version"] + 1)
                    payload["server"] = deepcopy(current)
                elif path == "/mcp/tokens":
                    assert body == {"scope_key": "onebot-v11:group:100", "bot_id": "200", "label": "新客户端", "lifetime_seconds": 3600}
                    payload["grant"] = {"token_id": "issued", "token": "mcp_one_time_only", "scope_key": body["scope_key"], "expires_at": 4000000000, "version": 1}
                    tokens.insert(0, {**payload["grant"], "label": body["label"], "bot_id": body["bot_id"], "revoked": False})
                    del tokens[0]["token"]
                elif path.endswith("/revoke"):
                    token = next(item for item in tokens if item["token_id"] == path.split("/")[3])
                    assert body == {"expected_version": token["version"]}
                    token.update(revoked=True, version=token["version"] + 1)
                else:
                    raise AssertionError(f"Unexpected mutation {path}")
            elif path == "/mcp":
                payload = {"configured": state["configured"], "servers": deepcopy(state["servers"]), "tokens": deepcopy(tokens),
                           "stdio_profiles": ["example-pinned"], "credential_refs": ["GAOJI_MCP_SECRET_EXAMPLE"],
                           "read_ready": True, "read_path": "/mcp", "resource": "mcp", "resource_version": state["version"]}
            elif path.startswith("/mcp/servers/"):
                current = next(item for item in state["servers"] if item["name"] == path.rsplit("/", 1)[-1])
                payload = {"server": deepcopy(current), "tools": deepcopy(tools), "resource": "mcp", "resource_version": state["version"]}
            elif path.startswith("/mcp/calls?"):
                from urllib.parse import parse_qs
                offset = int(parse_qs(path.split("?", 1)[1])["offset"][0])
                payload = {"items": calls[offset:offset + 5], "total": len(calls)}
            elif path == "/resource-versions":
                payload = {"versions": {"mcp": state["version"]}}
            await route.fulfill(json=payload)
        elif path == "/app.js" or path == "/app.css":
            await route.fulfill(body=(root / "admin-ui/dist" / path[1:]).read_bytes(), content_type="text/javascript" if path.endswith("js") else "text/css")
        elif path.startswith("/bot-admin"):
            await route.fulfill(content_type="text/html", body='<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1"><link rel="stylesheet" href="/app.css"></head><body><div id="root"></div><script type="module" src="/app.js"></script></body></html>')
        else:
            await route.abort()

    await context.route("**/*", route_handler)
    try:
        await page.goto("http://mcp-admin.test/bot-admin#mcp")
        await page.get_by_label("选择 MCP 服务器", exact=True).select_option("example")
        endpoint = page.get_by_label("MCP 服务端点", exact=True)
        await endpoint.wait_for()
        assert await endpoint.input_value() == server["config"]["url"]
        await endpoint.fill("https://edited.invalid/mcp")
        selector = page.get_by_label("MCP 凭证引用", exact=True)
        await selector.select_option("GAOJI_MCP_SECRET_EXAMPLE")
        await selector.focus()
        server["version"] += 1
        state["version"] += 1
        state["changed"] = True
        await page.get_by_role("button", name="载入最新配置", exact=True).wait_for()
        assert await selector.evaluate("el => document.activeElement === el"), "SSE replaced focused input"
        assert await endpoint.input_value() == "https://edited.invalid/mcp", "SSE overwrote draft"
        assert await page.get_by_role("button", name="保存配置", exact=True).is_disabled()
        await page.get_by_role("button", name="载入最新配置", exact=True).click()
        await endpoint.fill("https://saved.invalid/mcp")
        await page.get_by_role("button", name="保存配置", exact=True).click()
        await page.get_by_role("button", name="读取工具目录", exact=True).click()
        await page.wait_for_function("document.querySelectorAll('.mcp-tool').length === 5")
        await page.get_by_title("下一页", exact=True).click()
        await page.wait_for_function("document.querySelectorAll('.mcp-tool').length === 2")
        await page.get_by_title("上一页", exact=True).click()
        await page.get_by_label("lookup_0 审批策略", exact=True).select_option("safe")
        await page.get_by_title("保存 lookup_0", exact=True).click()
        await page.wait_for_function("document.querySelector('.mcp-tool .mcp-meta').textContent === 'v2'")
        await page.evaluate("scrollTo(0, 0)")
        await page.screenshot(path=str(output / f"mcp-tools-{width}.png"), full_page=True)
        assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1"), "tools overflow"

        await page.get_by_title("登记 MCP 服务器", exact=True).click()
        await page.get_by_label("MCP 服务器名称", exact=True).fill("created")
        await page.get_by_label("MCP 服务端点", exact=True).fill("https://created.invalid/mcp")
        await page.get_by_label("MCP 允许群号", exact=True).fill("100, 101")
        await page.get_by_role("button", name="保存配置", exact=True).click()
        await page.wait_for_function("document.querySelector('[aria-label=\"选择 MCP 服务器\"]').value === 'created'")
        await page.get_by_role("button", name="读取工具目录", exact=True).click()

        await page.get_by_role("tab", name="群只读 Token", exact=True).click()
        await page.get_by_label("Token 目标群号", exact=True).fill("100")
        await page.get_by_label("Token 机器人 QQ", exact=True).fill("200")
        await page.get_by_label("Token 备注", exact=True).fill("新客户端")
        await page.get_by_label("Token 有效期", exact=True).select_option("3600")
        await page.get_by_role("button", name="创建 Token", exact=True).click()
        await page.get_by_label("新 Token", exact=True).wait_for()
        assert await page.get_by_label("新 Token", exact=True).input_value() == "mcp_one_time_only"
        assert "mcp_one_time_only" not in await page.evaluate("JSON.stringify({localStorage, sessionStorage})")
        await page.get_by_title("撤销 新客户端", exact=True).click()
        await page.get_by_label("新 Token", exact=True).wait_for(state="detached")
        await page.get_by_title("下一页", exact=True).click()
        await page.wait_for_function("document.querySelectorAll('.mcp-token-row').length === 3")
        await page.evaluate("scrollTo(0, 0)")
        await page.screenshot(path=str(output / f"mcp-tokens-{width}.png"), full_page=True)
        assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1"), "tokens overflow"
        await page.get_by_role("tab", name="调用回执", exact=True).click()
        await page.wait_for_function("document.querySelectorAll('.mcp-call').length === 5")
        assert "example / lookup_0" in await page.locator(".mcp-call").first.inner_text()
        await page.locator(".mcp-call summary").first.click()
        assert not await page.evaluate("Boolean(window.unsafe)")
        await page.get_by_title("下一页", exact=True).click()
        await page.wait_for_function("document.querySelectorAll('.mcp-call').length === 4")
        await page.evaluate("scrollTo(0, 0)")
        await page.screenshot(path=str(output / f"mcp-calls-{width}.png"), full_page=True)
        assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1"), "calls overflow"
        state["configured"] = False
        state["changed"] = True
        await page.get_by_text("MCP 未启用", exact=True).wait_for()
        assert not errors, errors
        assert len(writes) == 7, writes
        print(f"{width}x{height}: CRUD/CAS, SSE draft/focus, pagination, one-time token, revoked token, bounded text: OK", flush=True)
    finally:
        await context.close()


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--browser", default="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
    parser.add_argument("--output", type=Path, default=Path(".cache/roadmap/mcp-ui"))
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(executable_path=args.browser, headless=True)
        try:
            for width, height in [(1280, 900), (390, 844)]:
                await verify(browser, Path(__file__).resolve().parents[1], args.output, width, height)
        finally:
            await browser.close()


if __name__ == "__main__":
    asyncio.run(main())
