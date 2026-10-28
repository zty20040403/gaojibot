from __future__ import annotations

import asyncio
import hashlib
import html
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any

from src.browser_runtime import (
    BrowserManager as BrowserManager,
    BrowserPolicyError as BrowserPolicyError,
    BrowserSession as BrowserSession,
    BrowserUnavailable as BrowserUnavailable,
)


class CodeSnapUnavailable(RuntimeError):
    pass


class RichMessageRenderer:
    """Render fenced code and Markdown tables to PNG, falling back cleanly."""

    def __init__(
        self,
        *,
        executable_path: str = "",
        timeout_seconds: int = 20,
        codesnap_enabled: bool = True,
        codesnap_executable_path: str = "codesnap",
        codesnap_config_path: str = "",
        codesnap_font_family: str = "Sarasa Mono SC",
        codesnap_theme: str = "candy",
        codesnap_timeout_seconds: int = 12,
        codesnap_cache_root: str | Path | None = None,
        codesnap_cache_entries: int = 256,
    ) -> None:
        self.executable_path = executable_path.strip()
        self.timeout_ms = min(max(int(timeout_seconds), 5), 60) * 1000
        self.codesnap_enabled = bool(codesnap_enabled)
        self.codesnap_executable_path = codesnap_executable_path.strip()
        self.codesnap_config_path = codesnap_config_path.strip()
        self.codesnap_font_family = codesnap_font_family.strip()
        self.codesnap_theme = codesnap_theme.strip()
        self.codesnap_timeout_seconds = min(
            max(int(codesnap_timeout_seconds), 3), 60
        )
        self.codesnap_cache_root = Path(
            codesnap_cache_root or Path(tempfile.gettempdir()) / "qq-bot-codesnap"
        )
        self.codesnap_cache_root.mkdir(parents=True, exist_ok=True)
        self.codesnap_cache_entries = min(
            max(int(codesnap_cache_entries), 16), 2048
        )
        self._playwright: Any = None
        self._browser: Any = None
        self._render_lock: asyncio.Lock | None = None
        self._codesnap_semaphore: asyncio.Semaphore | None = None

    async def close(self) -> None:
        async with self._lock():
            browser, playwright = self._browser, self._playwright
            self._browser = None
            self._playwright = None
        if browser is not None:
            await browser.close()
        if playwright is not None:
            await playwright.stop()

    async def render(self, text: str) -> bytes | None:
        block = parse_rich_block(text)
        if block is None:
            return None
        kind, language, source = block
        if kind == "code" and self.codesnap_enabled:
            return await self._render_codesnap(source, language)
        markup = _render_code_html(source, language) if kind == "code" else _render_table_html(source)
        if markup is None:
            return None
        async with self._lock():
            await self._ensure_browser()
            page = await self._browser.new_page(
                viewport={"width": 1400, "height": 900},
                device_scale_factor=2,
            )
            try:
                await page.set_content(markup, wait_until="load", timeout=self.timeout_ms)
                target = page.locator("#capture")
                return await target.screenshot(type="png", timeout=self.timeout_ms)
            finally:
                await page.close()

    async def _render_codesnap(self, source: str, language: str) -> bytes:
        executable = shutil.which(self.codesnap_executable_path)
        if executable is None:
            raise CodeSnapUnavailable(
                f"CodeSnap executable was not found: {self.codesnap_executable_path}"
            )
        if not source.strip():
            raise ValueError("CodeSnap cannot render an empty code block")
        if (
            len(source) > 30_000
            or source.count("\n") >= 400
            or any(len(line) > 500 for line in source.splitlines())
        ):
            raise ValueError("CodeSnap input is too large for a QQ image")
        normalized_language = _codesnap_language(language)
        cache_key = self._codesnap_cache_key(source, normalized_language)
        cached_path = self.codesnap_cache_root / f"{cache_key}.png"
        cached = await asyncio.to_thread(_read_valid_png, cached_path)
        if cached is not None:
            return cached

        async with self._codesnap_limit():
            cached = await asyncio.to_thread(_read_valid_png, cached_path)
            if cached is not None:
                return cached
            fd, temporary_name = tempfile.mkstemp(
                prefix=f"{cache_key}.",
                suffix=".png",
                dir=self.codesnap_cache_root,
            )
            os.close(fd)
            temporary_path = Path(temporary_name)
            temporary_path.unlink(missing_ok=True)
            command = [
                executable,
                "--from-code",
                "--output",
                str(temporary_path),
                "--silent",
                "--has-line-number",
                "--has-breadcrumbs",
                "false",
                "--mac-window-bar",
                "true",
                "--scale-factor",
                "2",
            ]
            if normalized_language:
                command.extend(("--language", normalized_language))
            if self.codesnap_font_family:
                command.extend(
                    ("--code-font-family", self.codesnap_font_family)
                )
            if self.codesnap_theme:
                command.extend(("--code-theme", self.codesnap_theme))
            if self.codesnap_config_path:
                command.extend(("--config", self.codesnap_config_path))
            codesnap_home = self.codesnap_cache_root / "home"
            codesnap_home.mkdir(parents=True, exist_ok=True)
            environment = os.environ.copy()
            environment["HOME"] = str(codesnap_home)
            environment["XDG_CONFIG_HOME"] = str(codesnap_home / ".config")
            try:
                process = await asyncio.create_subprocess_exec(
                    *command,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=environment,
                )
                try:
                    _stdout, stderr = await asyncio.wait_for(
                        process.communicate(source.encode("utf-8")),
                        timeout=self.codesnap_timeout_seconds,
                    )
                except TimeoutError:
                    process.kill()
                    await process.wait()
                    raise CodeSnapUnavailable(
                        f"CodeSnap exceeded {self.codesnap_timeout_seconds}s"
                    ) from None
                rendered = await asyncio.to_thread(
                    _read_valid_png,
                    temporary_path,
                )
                if process.returncode != 0 or rendered is None:
                    detail = stderr.decode("utf-8", errors="replace").strip()
                    raise CodeSnapUnavailable(
                        "CodeSnap did not produce a valid PNG"
                        + (f": {detail[:500]}" if detail else "")
                    )
                os.replace(temporary_path, cached_path)
                await asyncio.to_thread(
                    _trim_codesnap_cache,
                    self.codesnap_cache_root,
                    self.codesnap_cache_entries,
                )
                return rendered
            finally:
                temporary_path.unlink(missing_ok=True)

    def _codesnap_cache_key(self, source: str, language: str) -> str:
        config_fingerprint = ""
        if self.codesnap_config_path:
            try:
                config_fingerprint = hashlib.sha256(
                    Path(self.codesnap_config_path).read_bytes()
                ).hexdigest()
            except OSError:
                config_fingerprint = self.codesnap_config_path
        payload = "\0".join(
            (
                "codesnap-v1",
                language,
                self.codesnap_font_family,
                self.codesnap_theme,
                config_fingerprint,
                source,
            )
        )
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    async def _ensure_browser(self) -> None:
        if self._browser is not None:
            return
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:
            raise BrowserUnavailable(
                "Playwright is not installed; rich output will use text fallback"
            ) from exc
        self._playwright = await async_playwright().start()
        kwargs: dict[str, str] = {}
        if self.executable_path:
            kwargs["executable_path"] = self.executable_path
        self._browser = await self._playwright.chromium.launch(headless=True, **kwargs)

    def _lock(self) -> asyncio.Lock:
        if self._render_lock is None:
            self._render_lock = asyncio.Lock()
        return self._render_lock

    def _codesnap_limit(self) -> asyncio.Semaphore:
        if self._codesnap_semaphore is None:
            self._codesnap_semaphore = asyncio.Semaphore(2)
        return self._codesnap_semaphore


def parse_rich_block(text: str) -> tuple[str, str, str] | None:
    source = str(text).strip()
    fenced = re.fullmatch(r"```([^\n`]*)\n([\s\S]*?)\n```", source)
    if fenced is not None:
        return "code", fenced.group(1).strip()[:40], fenced.group(2)
    lines = [line.strip() for line in source.splitlines() if line.strip()]
    if len(lines) >= 2 and _is_table_separator(lines[1]):
        return "table", "", source
    return None


_CODESNAP_LANGUAGE_ALIASES = {
    "bash": "sh",
    "shell": "sh",
    "zsh": "sh",
    "console": "sh",
    "py": "python",
    "python3": "python",
    "js": "javascript",
    "jsx": "javascript",
    "ts": "typescript",
    "tsx": "typescript",
    "yml": "yaml",
    "rs": "rust",
    "c++": "cpp",
    "objective-c": "objectivec",
    "objc": "objectivec",
    "plaintext": "text",
    "txt": "text",
}


def _codesnap_language(language: str) -> str:
    value = language.strip().casefold()
    value = re.sub(r"[^a-z0-9_+#.-]", "", value)[:40]
    return _CODESNAP_LANGUAGE_ALIASES.get(value, value)


def _read_valid_png(path: Path) -> bytes | None:
    try:
        data = path.read_bytes()
    except OSError:
        return None
    if len(data) < 32 or not data.startswith(b"\x89PNG\r\n\x1a\n"):
        return None
    return data


def _trim_codesnap_cache(root: Path, max_entries: int) -> None:
    try:
        entries = sorted(
            root.glob("*.png"),
            key=lambda path: path.stat().st_mtime_ns,
            reverse=True,
        )
    except OSError:
        return
    for path in entries[max_entries:]:
        try:
            path.unlink()
        except OSError:
            pass


def _render_code_html(source: str, language: str) -> str:
    highlighted = ""
    try:
        from pygments import highlight
        from pygments.formatters import HtmlFormatter
        from pygments.lexers import TextLexer, get_lexer_by_name
        from pygments.util import ClassNotFound

        try:
            lexer = get_lexer_by_name(language) if language else TextLexer()
        except ClassNotFound:
            lexer = TextLexer()
        formatter = HtmlFormatter(nowrap=True)
        highlighted = highlight(source, lexer, formatter)
        syntax_css = formatter.get_style_defs(".code")
    except ImportError:
        highlighted = html.escape(source)
        syntax_css = ""
    line_count = max(source.count("\n") + 1, 1)
    numbers = "\n".join(str(index) for index in range(1, line_count + 1))
    return _html_shell(
        f"""
        <div id="capture" class="code-wrap">
          <pre class="numbers">{numbers}</pre>
          <pre class="code">{highlighted}</pre>
        </div>
        """,
        f"""
        {syntax_css}
        .code-wrap {{ display:flex; max-width:1280px; background:#11161d; color:#e7edf3;
          border:1px solid #27313b; border-radius:6px; overflow:hidden; }}
        pre {{ margin:0; padding:18px 20px; font:15px/1.55 "Sarasa Mono SC","Noto Sans Mono CJK SC",ui-monospace,SFMono-Regular,Menlo,monospace;
          white-space:pre; tab-size:4; letter-spacing:0; }}
        .numbers {{ color:#71808e; text-align:right; user-select:none; padding-right:12px;
          border-right:1px solid #27313b; background:#0d1218; }}
        .code {{ overflow:visible; }}
        """,
    )


def _render_table_html(source: str) -> str | None:
    rows = [_split_table_row(line) for line in source.splitlines() if line.strip()]
    if len(rows) < 2 or not _is_table_separator(source.splitlines()[1].strip()):
        return None
    header, body = rows[0], rows[2:]
    columns = max([len(header), *(len(row) for row in body)] or [1])
    header += [""] * (columns - len(header))
    body = [row + [""] * (columns - len(row)) for row in body]
    head_html = "".join(f"<th>{html.escape(cell)}</th>" for cell in header)
    body_html = "".join(
        "<tr>" + "".join(f"<td>{html.escape(cell)}</td>" for cell in row) + "</tr>"
        for row in body
    )
    return _html_shell(
        f'<div id="capture"><table><thead><tr>{head_html}</tr></thead><tbody>{body_html}</tbody></table></div>',
        """
        #capture { display:inline-block; max-width:1320px; background:white; }
        table { border-collapse:collapse; color:#182026; font:15px/1.45 "Noto Sans CJK SC","Sarasa Gothic SC","Source Han Sans SC","Microsoft YaHei","PingFang SC",system-ui,sans-serif; }
        th,td { border:1px solid #c8d0d5; padding:9px 12px; min-width:90px; max-width:360px;
          text-align:left; vertical-align:top; overflow-wrap:anywhere; letter-spacing:0; }
        th { background:#eef2f4; font-weight:650; }
        tr:nth-child(even) td { background:#f8fafb; }
        """,
    )


def _html_shell(body: str, css: str) -> str:
    return f"""<!doctype html><html><head><meta charset="utf-8"><style>
      html,body {{ margin:0; padding:0; background:transparent; }}
      body {{ display:inline-block; padding:18px; }}
      * {{ box-sizing:border-box; }}
      {css}
    </style></head><body>{body}</body></html>"""


def _is_table_separator(line: str) -> bool:
    cells = _split_table_row(line)
    return bool(cells) and all(re.fullmatch(r":?-{3,}:?", cell) for cell in cells)


def _split_table_row(line: str) -> list[str]:
    value = line.strip().strip("|")
    cells = re.split(r"(?<!\\)\|", value)
    return [cell.replace("\\|", "|").strip() for cell in cells]
