"""Offline image acceptance, before an installer creates user data or services."""
from __future__ import annotations

import hashlib
import importlib
import json
import os
from pathlib import Path, PurePosixPath
import subprocess


ROOT_FILES = {"pyproject.toml", "uv.lock", "README.md", "alembic.ini", "LICENSE", "THIRD_PARTY_NOTICES.md"}
# These exact files are verified during bundle extraction on the host; they
# build optional containers and are not copied into the bot runtime image.
BUILD_ONLY = {"Dockerfile", "Desktop.Dockerfile", "images.lock.json", "applications.json", "sandbox/default.nix", "sandbox/build.sh",
    "sandbox/builder.lock.json", "sandbox/image.nix", "vendor/whisper.cpp-v1.9.4.tar.gz"}


def verify_sources(root: Path) -> int:
    manifest = json.loads((root / "manifest.json").read_text())
    if manifest.get("schema") != 1 or not isinstance(manifest.get("files"), list):
        raise ValueError("Invalid runtime manifest")
    seen: set[str] = set()
    checked = 0
    for item in manifest["files"]:
        name = item["path"]
        relative = PurePosixPath(name)
        if not name or relative.is_absolute() or ".." in relative.parts or "\\" in name or name in seen:
            raise ValueError("Invalid runtime manifest path")
        seen.add(name)
        if name in BUILD_ONLY:
            continue
        if name not in ROOT_FILES and relative.parts[0] not in {"src", "migrations", "skills"}:
            raise ValueError("Unexpected runtime manifest file")
        filename = root / relative
        if filename.is_symlink() or not filename.resolve().is_relative_to(root.resolve()):
            raise ValueError("Runtime file is outside the verified image")
        contents = filename.read_bytes()
        if len(contents) != item["bytes"] or hashlib.sha256(contents).hexdigest() != item["sha256"]:
            raise ValueError("Runtime file verification failed")
        if filename.suffix == ".py":
            compile(contents, name, "exec")
        checked += 1
    required = ROOT_FILES | {"src/__init__.py", "src/local_runtime/entrypoint.py", "migrations/env.py",
        "src/plugins/ai_chat/admin_ui_dist/app.js", "src/plugins/ai_chat/admin_ui_dist/app.css"}
    if not required <= seen:
        raise ValueError("Runtime source or console assets are missing")
    return checked


def check_runtime() -> dict:
    if os.geteuid() != 10001:
        raise ValueError("Image acceptance must use the unprivileged runtime account")
    count = verify_sources(Path("/opt/gaoji"))
    for module in ("nonebot", "nonebot.adapters.onebot.v11", "psycopg", "psycopg_pool", "sqlalchemy",
        "alembic", "argon2", "httpx", "openai", "edge_tts", "pysilk", "miniaudio", "playwright.sync_api"):
        importlib.import_module(module)
    for command in (["ffmpeg", "-version"], ["pdftotext", "-v"], ["/usr/bin/chromium", "--version"], ["whisper-cli", "--help"]):
        subprocess.run(command, check=True, capture_output=True, timeout=15)
    family = subprocess.check_output(["fc-match", "-f", "%{family}", "sans:lang=zh-cn"], timeout=15, text=True)
    if "Noto" not in family or "CJK" not in family:
        raise ValueError("Chinese font is missing from the runtime")
    from playwright.sync_api import sync_playwright
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path="/usr/bin/chromium", timeout=30000)
        try:
            page = browser.new_page(viewport={"width": 320, "height": 240})
            page.set_content('<canvas width="320" height="240"></canvas>')
            rendered = page.evaluate("""() => {
                const canvas = document.querySelector('canvas');
                const ctx = canvas.getContext('2d');
                ctx.fillStyle = '#fff'; ctx.fillRect(0, 0, 320, 240);
                ctx.fillStyle = '#222'; ctx.font = '32px sans-serif';
                ctx.fillText(String.fromCodePoint(0x4e2d, 0x6587), 20, 60);
                return ctx.getImageData(0, 0, 320, 240).data.some((v, i) => i % 4 === 0 && v < 128);
            }""")
            screenshot = page.screenshot()
            if not rendered or not screenshot.startswith(b"\x89PNG\r\n\x1a\n"):
                raise ValueError("Browser did not render the acceptance page")
        finally:
            browser.close()
    return {"sources_checked": count, "runtime_uid": os.geteuid(), "dependencies": "ok", "browser": "ok", "chinese_font": "ok"}


def check_video(model_path: Path | None = None, *, executable: str = "whisper-cli") -> dict:
    import tempfile
    import wave
    from .video_assets import CONTAINER_MODEL_PATH, MODEL_BYTES, MODEL_SHA256
    model = model_path or Path(CONTAINER_MODEL_PATH)
    if model.is_symlink() or not model.is_file() or model.stat().st_size != MODEL_BYTES:
        raise ValueError("Video model is missing or invalid")
    with model.open("rb") as source:
        if hashlib.file_digest(source, "sha256").hexdigest() != MODEL_SHA256:
            raise ValueError("Video model verification failed")
    with tempfile.TemporaryDirectory(prefix="gaoji-video-check-") as directory:
        audio = Path(directory) / "silence.wav"
        with wave.open(str(audio), "wb") as output:
            output.setnchannels(1); output.setsampwidth(2); output.setframerate(16000)
            output.writeframes(b"\0" * (16000 * 2 * 2))
        # This gate checks model loading and one bounded inference, not accuracy.
        # Full-context beam search on silence is slow on generic/emulated CPUs.
        subprocess.run([executable, "-m", str(model), "-f", str(audio), "-t", "1", "-l", "en",
            "-ac", "128", "-bo", "1", "-bs", "1", "-nf", "-ng", "-np", "-nt"],
            check=True, capture_output=True, timeout=120)
    return {"video_ready": True, "model": "whisper-base", "sha256": MODEL_SHA256}
