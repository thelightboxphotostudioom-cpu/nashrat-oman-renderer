import asyncio
import json
import os
import shutil
import tempfile
from contextlib import asynccontextmanager
from datetime import datetime
from zoneinfo import ZoneInfo

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import Response
from jinja2 import Environment, FileSystemLoader
from playwright.async_api import async_playwright
from pydantic import BaseModel, Field

ROOT = os.path.dirname(os.path.abspath(__file__))

with open(f"{ROOT}/assets/tajawal_base64.css", encoding="utf-8") as f:
    TAJAWAL_CSS = f.read()
with open(f"{ROOT}/assets/khanjar_base64.txt", encoding="utf-8") as f:
    LOGO_B64 = f.read()
with open(f"{ROOT}/assets/khanjar_navy_base64.txt", encoding="utf-8") as f:
    LOGO_NAVY_B64 = f.read()

ENV = Environment(loader=FileSystemLoader(f"{ROOT}/templates"))
TEMPLATE = ENV.get_template("post.html")
VIDEO_TEMPLATE = ENV.get_template("post_video.html")

ARABIC_MONTHS = [
    "", "يناير", "فبراير", "مارس", "أبريل", "مايو", "يونيو",
    "يوليو", "أغسطس", "سبتمبر", "أكتوبر", "نوفمبر", "ديسمبر",
]


class RenderRequest(BaseModel):
    headline: str
    summary: str | None = None
    source: str | None = None
    category_emoji: str = "📰"
    category_label: str = "خبر"
    photo_url: str | None = None
    photo_fit: str = "cover"  # "cover" for real photos, "contain" for logos/graphics with their own bg
    photo_bg: str = "#FFFFFF"  # canvas bg behind a "contain"-fit graphic
    photo_tag_text: str | None = None  # e.g. "صورة حقيقية" / "صورة أرشيفية"
    ai_disclaimer: bool = False
    is_mourning: bool = False
    date_text: str | None = None
    file_name: str | None = None
    token: str | None = Field(default=None, description="shared secret, optional")


class VideoRenderRequest(BaseModel):
    headline: str
    summary: str | None = None
    source: str | None = None
    category_emoji: str = "📰"
    category_label: str = "خبر"
    video_url: str
    date_text: str | None = None
    file_name: str | None = None
    token: str | None = Field(default=None, description="shared secret, optional")


RENDER_TOKEN = os.environ.get("RENDER_SHARED_TOKEN")  # optional simple auth

_playwright = None
_browser = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _playwright, _browser
    _playwright = await async_playwright().start()
    _browser = await _playwright.chromium.launch(
        args=["--no-sandbox", "--disable-dev-shm-usage"]
    )
    yield
    await _browser.close()
    await _playwright.stop()


app = FastAPI(lifespan=lifespan)


def default_date_text() -> str:
    now = datetime.now(ZoneInfo("Asia/Muscat"))
    return f"{now.day} {ARABIC_MONTHS[now.month]} {now.year}"


async def _download(url: str, dest: str) -> None:
    async with httpx.AsyncClient(follow_redirects=True, timeout=60.0) as client:
        async with client.stream("GET", url) as resp:
            resp.raise_for_status()
            with open(dest, "wb") as f:
                async for chunk in resp.aiter_bytes(1 << 16):
                    f.write(chunk)


async def _run(*args: str, timeout: float = 120.0) -> tuple[bytes, bytes]:
    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    if proc.returncode != 0:
        raise HTTPException(
            status_code=502,
            detail=f"{args[0]} failed (code {proc.returncode}): {err.decode(errors='ignore')[-800:]}",
        )
    return out, err


async def _probe(path: str) -> dict:
    out, _ = await _run(
        "ffprobe", "-v", "error", "-print_format", "json",
        "-show_format", "-show_streams", path,
    )
    data = json.loads(out)
    v_stream = next((s for s in data.get("streams", []) if s.get("codec_type") == "video"), None)
    has_audio = any(s.get("codec_type") == "audio" for s in data.get("streams", []))
    if not v_stream:
        raise HTTPException(status_code=400, detail="source has no video stream")
    width = int(v_stream["width"])
    height = int(v_stream["height"])
    # respect rotation metadata if present so a sideways phone clip reports correctly
    rotation = 0
    try:
        rotation = int(v_stream.get("tags", {}).get("rotate", 0))
    except (TypeError, ValueError):
        rotation = 0
    side_data = v_stream.get("side_data_list") or []
    for sd in side_data:
        rot = sd.get("rotation")
        if rot is not None:
            rotation = int(rot)
    if abs(rotation) in (90, 270):
        width, height = height, width
    duration = float(data.get("format", {}).get("duration") or v_stream.get("duration") or 0)
    return {"width": width, "height": height, "duration": duration, "has_audio": has_audio}


@app.get("/health")
async def health():
    return {"ok": True}


@app.post("/render")
async def render(req: RenderRequest):
    if RENDER_TOKEN and req.token != RENDER_TOKEN:
        raise HTTPException(status_code=401, detail="invalid token")

    html = TEMPLATE.render(
        tajawal_css=TAJAWAL_CSS,
        logo_b64=LOGO_B64,
        logo_navy_b64=LOGO_NAVY_B64,
        photo_url=req.photo_url,
        photo_fit=req.photo_fit,
        photo_bg=req.photo_bg,
        category_emoji=req.category_emoji,
        category_label=req.category_label,
        photo_tag_text=req.photo_tag_text,
        ai_disclaimer=req.ai_disclaimer,
        is_mourning=req.is_mourning,
        headline=req.headline,
        summary=req.summary,
        source=req.source,
        date_text=req.date_text or default_date_text(),
    )

    page = await _browser.new_page(viewport={"width": 1080, "height": 1080})
    try:
        await page.set_content(html, wait_until="networkidle", timeout=20000)
        png_bytes = await page.screenshot(type="png")
    finally:
        await page.close()

    headers = {}
    if req.file_name:
        headers["Content-Disposition"] = f'inline; filename="{req.file_name}.png"'
    return Response(content=png_bytes, media_type="image/png", headers=headers)


# Reel-safe canvas: Instagram republishes every video as a Reel regardless of
# the source's own aspect ratio, so every branded video we hand back is
# already a 1080x1920 frame. A portrait/near-9:16 source fills it edge to
# edge (same look as the existing full-bleed photo template); a square or
# landscape source is shown un-cropped inside a centered window instead of
# being stretched or cropped to fit.
VIDEO_CANVAS_W = 1080
VIDEO_CANVAS_H = 1920
MAX_CLIP_SECONDS = 90  # Instagram's own Reel cap


@app.post("/render-video")
async def render_video(req: VideoRenderRequest):
    if RENDER_TOKEN and req.token != RENDER_TOKEN:
        raise HTTPException(status_code=401, detail="invalid token")

    tmpdir = tempfile.mkdtemp(prefix="nvr_")
    try:
        src_path = os.path.join(tmpdir, "src.bin")
        await _download(req.video_url, src_path)

        info = await _probe(src_path)
        width, height, duration, has_audio = (
            info["width"], info["height"], info["duration"], info["has_audio"]
        )
        if duration <= 0:
            duration = 10.0
        duration = min(duration, MAX_CLIP_SECONDS)
        aspect = width / height if height else 1.0

        video_mode = "cover" if aspect <= 0.8 else "window"

        window_left = window_top = window_draw_w = window_draw_h = 0
        if video_mode == "window":
            margin = 60
            top_safe = 170
            bottom_reserve = 460
            avail_h = VIDEO_CANVAS_H - top_safe - bottom_reserve
            draw_w = VIDEO_CANVAS_W - 2 * margin
            draw_h = draw_w * height / width
            if draw_h > avail_h:
                scale = avail_h / draw_h
                draw_h = avail_h
                draw_w = draw_w * scale
            window_top = int(top_safe + (avail_h - draw_h) / 2)
            window_left = int((VIDEO_CANVAS_W - draw_w) / 2)
            window_draw_w = int(draw_w)
            window_draw_h = int(draw_h)

        # Earlier approach played the clip live inside the page and had
        # Chromium record the composited result as video - that 502'd on
        # Render's 512MB free instance even after --disable-dev-shm-usage and
        # a half-resolution capture: Chromium's own video-encode pipeline is
        # just too heavy for that budget. Chromium now renders the branded
        # chrome ONCE as a transparent still PNG (the same cheap cost as the
        # existing, already-stable /render image endpoint - no <video>
        # element, no recording), and ffmpeg alone - much lighter than
        # Chromium's compositor - does all the actual video work: scaling
        # the source clip and compositing it under the chrome PNG.
        chrome_png = os.path.join(tmpdir, "chrome.png")
        html = VIDEO_TEMPLATE.render(
            tajawal_css=TAJAWAL_CSS,
            logo_b64=LOGO_B64,
            logo_navy_b64=LOGO_NAVY_B64,
            canvas_w=VIDEO_CANVAS_W,
            canvas_h=VIDEO_CANVAS_H,
            video_mode=video_mode,
            window_top=window_top,
            window_left=window_left,
            window_draw_w=window_draw_w,
            window_draw_h=window_draw_h,
            category_emoji=req.category_emoji,
            category_label=req.category_label,
            headline=req.headline,
            summary=req.summary,
            source=req.source,
            date_text=req.date_text or default_date_text(),
        )
        page = await _browser.new_page(
            viewport={"width": VIDEO_CANVAS_W, "height": VIDEO_CANVAS_H}
        )
        try:
            await page.set_content(html, wait_until="networkidle", timeout=20000)
            await page.screenshot(path=chrome_png, omit_background=True)
        finally:
            await page.close()

        final_mp4 = os.path.join(tmpdir, "final.mp4")
        if video_mode == "cover":
            # Video fills the whole canvas edge-to-edge, same crop math as
            # the CSS object-fit:cover it replaces.
            filter_complex = (
                f"[0:v]scale={VIDEO_CANVAS_W}:{VIDEO_CANVAS_H}:"
                f"force_original_aspect_ratio=increase,"
                f"crop={VIDEO_CANVAS_W}:{VIDEO_CANVAS_H}[clip];"
                f"[clip][1:v]overlay=0:0:format=auto[outv]"
            )
            mux_args = [
                "ffmpeg", "-y",
                "-i", src_path,
                "-loop", "1", "-t", str(duration), "-i", chrome_png,
                "-filter_complex", filter_complex,
                "-map", "[outv]",
            ]
            if has_audio:
                mux_args += ["-map", "0:a:0", "-c:a", "aac", "-b:a", "128k"]
        else:
            # Navy canvas, the clip scaled to fit un-cropped inside the
            # window rect (object-fit:contain), then the chrome PNG - whose
            # window-rect area is left unpainted/transparent so the clip
            # shows through - on top.
            filter_complex = (
                f"[2:v]scale={window_draw_w}:{window_draw_h}[clip];"
                f"[0:v][clip]overlay={window_left}:{window_top}[bg];"
                f"[bg][1:v]overlay=0:0:format=auto[outv]"
            )
            mux_args = [
                "ffmpeg", "-y",
                "-f", "lavfi", "-i",
                f"color=c=#0B1F3A:s={VIDEO_CANVAS_W}x{VIDEO_CANVAS_H}:"
                f"d={duration}:r=25",
                "-loop", "1", "-t", str(duration), "-i", chrome_png,
                "-i", src_path,
                "-filter_complex", filter_complex,
                "-map", "[outv]",
            ]
            if has_audio:
                mux_args += ["-map", "2:a:0", "-c:a", "aac", "-b:a", "128k"]
        mux_args += [
            "-t", str(duration),
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-profile:v", "high",
            "-movflags", "+faststart", final_mp4,
        ]
        await _run(*mux_args, timeout=120.0)

        with open(final_mp4, "rb") as f:
            mp4_bytes = f.read()
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)

    headers = {}
    if req.file_name:
        headers["Content-Disposition"] = f'inline; filename="{req.file_name}.mp4"'
    return Response(content=mp4_bytes, media_type="video/mp4", headers=headers)
