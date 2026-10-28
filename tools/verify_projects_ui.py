"""Exercise the built project console with intercepted APIs, without servers."""

import argparse
import asyncio
from copy import deepcopy
import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from playwright.async_api import async_playwright


async def verify(browser, root, output, width, height):
    context = await browser.new_context(viewport={"width": width, "height": height})
    page = await context.new_page()
    errors, writes = [], []
    page.on("pageerror", lambda error: errors.append(str(error)))
    now = 1793180000
    projects = [{"project_id": index, "title": f"项目 {index} · " + "长期保留的网页和分析报告" * 8,
                 "scope_key": "onebot-v11:group:100000001", "status": "stopped", "backend": "oci",
                 "pinned": False, "version": 1, "used_bytes": 4096, "quota_bytes": 2 * 1024**3,
                 "quota_mode": "soft", "usage_fresh": index != 2, "last_known_used_bytes": 4096,
                 "rebuildable_bytes": 1024, "version_bytes": 8192, "sampled_at": now,
                 "quota_editable": index == 1, "workspace_generation": 0 if index == 1 else 1,
                 "lifecycle_job_id": None, "locked_task_id": None} for index in range(1, 8)]
    scope = {"quota_bytes": 8 * 1024**3, "max_projects": 10, "version": 1,
             "accounted_bytes": 98304, "reserved_bytes": 4096, "project_count": 7}
    versions = [{"version_no": number, "label": "报告版 <script>window.unsafe=true</script>",
                 "pinned": False, "file_count": 12, "total_bytes": 8192, "created_at": now}
                for number in range(7, 0, -1)]
    previews = [{"preview_id": "preview-" + str(index) * 30, "version_no": 1, "root": "dist/" + "long-path-" * 16,
                 "expires_at": now + 604800, "created_at": now, "revoked_at": None} for index in range(7)]
    events = [{"event_id": index, "kind": "admin_pin", "actor_principal_id": None, "created_at": now} for index in range(7)]
    state = {"version": 0, "sequence": 0, "changed": False, "available": True}
    operation = None

    async def route_handler(route):
        nonlocal operation
        url = urlsplit(route.request.url)
        path, query = url.path, parse_qs(url.query)
        if path.startswith("/bot-admin/api/v1"):
            path = path.removeprefix("/bot-admin/api/v1")
            payload = {}
            if path == "/me":
                payload = {"account": {"username": "fixture", "role": "admin"}}
            elif path == "/events":
                state["sequence"] += 1
                event = {"type": "resources.changed" if state["changed"] else "ready", "sequence": state["sequence"],
                         "resources": ["projects"], "versions": {"projects": state["version"]}}
                state["changed"] = False
                await route.fulfill(content_type="text/event-stream", body="data: " + json.dumps(event) + "\n\n")
                return
            elif path.startswith("/projects/"):
                project = projects[int(path.split("/")[2]) - 1]
                if route.request.method in {"PUT", "POST"}:
                    body = route.request.post_data_json
                    assert route.request.headers["if-match"] == f'"{state["version"]}"'
                    assert body["expected_version"] == project["version"]
                    action = path.rsplit("/", 1)[-1]
                    if "/versions/" in path:
                        number = int(path.split("/")[4])
                        next(item for item in versions if item["version_no"] == number)["pinned"] = body["pinned"]
                    elif action == "pin":
                        project["pinned"] = body["pinned"]
                    elif action == "quota":
                        assert project["quota_editable"]
                        if project.get("quota_requires_job"):
                            operation = {"job_id": 501, "action": "resize", "job_status": "pending",
                                "from_quota_bytes": project["quota_bytes"], "quota_bytes": body["quota_bytes"],
                                "completed_steps": 0, "total_steps": 5, "phase": "lifecycle", "updated_at": now}
                            project.update(quota_editable=False, lifecycle_job_id=501)
                        else:
                            project["quota_bytes"] = body["quota_bytes"]
                    elif action == "scope-limits":
                        assert body["scope_version"] == scope["version"]
                        scope.update(quota_bytes=body["quota_bytes"], max_projects=body["max_projects"], version=scope["version"] + 1)
                    elif action == "revoke":
                        next(item for item in previews if item["preview_id"] == path.split("/")[4])["revoked_at"] = now
                    else:
                        raise AssertionError(path)
                    project["version"] += 1
                    state["version"] += 1
                    writes.append((path, body))
                    payload = {"resource": "projects", "resource_version": state["version"], "change": {"version": project["version"]}}
                else:
                    section, offset = query.get("section", ["versions"])[0], int(query.get("offset", [0])[0])
                    rows = {"versions": versions, "previews": previews, "events": events}[section]
                    payload = {"project": deepcopy(project), "scope_limits": deepcopy(scope), "operation": deepcopy(operation),
                               "section": section, "total": len(rows), "items": deepcopy(rows[offset:offset + 5])}
            elif path == "/projects":
                filtered = [item for item in projects if query.get("query", [""])[0] in item["title"]
                            and (not query.get("state") or item["status"] == query["state"][0])]
                offset = int(query.get("offset", [0])[0])
                payload = {"configured": True, "available": state["available"], "resource": "projects",
                           "resource_version": state["version"], "counts": {"stopped": 7},
                           "total": len(filtered), "items": deepcopy(filtered[offset:offset + 5])}
            elif path == "/resource-versions":
                payload = {"versions": {"projects": state["version"]}}
            await route.fulfill(json=payload)
        elif path in {"/app.js", "/app.css"}:
            await route.fulfill(body=(root / "admin-ui/dist" / path[1:]).read_bytes(),
                                content_type="text/javascript" if path.endswith("js") else "text/css")
        elif path == "/bot-admin":
            await route.fulfill(content_type="text/html", body='<!doctype html><html><head><meta name="viewport" content="width=device-width,initial-scale=1"><link rel="stylesheet" href="/app.css"></head><body><div id="root"></div><script type="module" src="/app.js"></script></body></html>')
        else:
            await route.abort()

    await context.route("**/*", route_handler)
    try:
        await page.goto("http://project-admin.test/bot-admin#projects")
        await page.wait_for_function("document.querySelectorAll('.project-row').length === 5")
        selector = page.get_by_label("项目状态", exact=True)
        await selector.focus()
        state["changed"] = True
        await page.wait_for_timeout(1500)
        assert await selector.evaluate("el => el === document.activeElement"), "SSE replaced state selector"
        await page.get_by_title("下一页", exact=True).click()
        await page.wait_for_function("document.querySelectorAll('.project-row').length === 2")
        await page.get_by_title("上一页", exact=True).click()
        await page.wait_for_function("document.querySelectorAll('.project-row').length === 5")
        await page.screenshot(path=str(output / f"projects-list-{width}.png"), full_page=True)
        assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1"), "list overflow"
        await page.get_by_title("打开 prj#1", exact=True).click()
        field = page.get_by_label("会话总额度 MiB", exact=True)
        await field.fill("10000")
        await field.focus()
        projects[0]["version"] += 1
        state["version"] += 1
        state["changed"] = True
        await page.get_by_role("button", name="载入最新配置", exact=True).wait_for()
        assert await field.input_value() == "10000", "SSE overwrote quota draft"
        assert await field.evaluate("el => el === document.activeElement"), "SSE replaced quota input"
        assert await page.get_by_role("button", name="保存会话额度", exact=True).is_disabled()
        await page.get_by_role("button", name="载入最新配置", exact=True).click()
        await field.fill("10000")
        await page.get_by_label("项目配额 MiB", exact=True).fill("3072")
        await page.get_by_role("button", name="保存项目配额", exact=True).click()
        await page.wait_for_function("document.querySelector('.projects-view').textContent.includes('配置 v3')")
        assert await field.input_value() == "10000", "saving project quota discarded unsaved scope quota"
        await page.get_by_role("button", name="保存会话额度", exact=True).click()
        await page.wait_for_function("document.querySelector('.projects-view').textContent.includes('会话配置 v2')")
        await page.get_by_title("固定项目", exact=True).click()
        await page.get_by_title("取消固定项目", exact=True).wait_for()
        await page.get_by_title("固定 v7", exact=True).click()
        await page.get_by_title("取消固定 v7", exact=True).wait_for()
        assert not await page.evaluate("Boolean(window.unsafe)")
        await page.get_by_role("tab", name="预览记录", exact=True).click()
        await page.get_by_title("撤销 " + previews[0]["preview_id"], exact=True).click()
        await page.wait_for_function("document.querySelector('.project-record').textContent.includes('已撤销')")
        await page.screenshot(path=str(output / f"projects-detail-{width}.png"), full_page=True)
        assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1"), "detail overflow"
        await page.get_by_title("下一页", exact=True).click()
        await page.wait_for_function("document.querySelectorAll('.project-record').length === 2")
        await page.get_by_role("tab", name="操作记录", exact=True).click()
        await page.wait_for_function("document.querySelectorAll('.project-record').length === 5")
        await page.get_by_role("button", name="全部项目", exact=True).click()
        await page.get_by_title("打开 prj#2", exact=True).click()
        assert await page.get_by_label("项目配额 MiB", exact=True).is_disabled()
        assert await page.get_by_text("当前工作区暂不支持扩缩容", exact=True).is_visible()
        projects[1].update(backend="vm", quota_mode="hard", quota_editable=True, quota_requires_job=True)
        state["changed"] = True
        field = page.get_by_label("项目配额 MiB", exact=True)
        await field.fill("4096")
        await page.get_by_role("button", name="提交容量调整", exact=True).click()
        await page.get_by_text("调整磁盘容量", exact=False).wait_for()
        assert projects[1]["quota_bytes"] == 2 * 1024**3, "queued job must not claim new capacity"
        assert await field.is_disabled()
        operation.update(job_status="running", completed_steps=3)
        state["changed"] = True
        await page.get_by_text("3/5 步", exact=True).wait_for()
        scope_field = page.get_by_label("会话总额度 MiB", exact=True)
        await scope_field.fill("12000")
        await scope_field.focus()
        await page.screenshot(path=str(output / f"projects-resize-{width}.png"), full_page=True)
        assert await page.evaluate("document.documentElement.scrollWidth <= innerWidth + 1"), "resize overflow"
        projects[1].update(quota_bytes=4 * 1024**3, quota_editable=True, lifecycle_job_id=None, version=projects[1]["version"] + 1)
        operation = None
        state["changed"] = True
        await page.get_by_role("button", name="载入最新配置", exact=True).wait_for()
        assert await scope_field.input_value() == "12000", "completion discarded another quota draft"
        assert await scope_field.evaluate("el => el === document.activeElement"), "completion stole focus"
        state["available"], state["changed"] = False, True
        await page.get_by_text("项目运行环境尚未就绪", exact=True).wait_for()
        assert not errors, errors
        assert len(writes) == 6, writes
        print(f"{width}x{height}: paging, metadata writes, SSE focus/drafts, conflict, stale usage, escaped labels, layout OK", flush=True)
    finally:
        await context.close()


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--browser", default="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
    parser.add_argument("--output", type=Path, default=Path(".cache/roadmap/project-admin-ui"))
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
