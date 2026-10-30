import base64
import os
from contextlib import asynccontextmanager
from datetime import datetime
from zoneinfo import ZoneInfo

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
    photo_tag_text: str | None = None  # e.g. "صورة حقيقية" / "صورة أرشيفية"
    ai_disclaimer: bool = False
    is_mourning: bool = False
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
    _browser = await _playwright.chromium.launch(args=["--no-sandbox"])
    yield
    await _browser.close()
    await _playwright.stop()


app = FastAPI(lifespan=lifespan)


def default_date_text() -> str:
    now = datetime.now(ZoneInfo("Asia/Muscat"))
    return f"{now.day} {ARABIC_MONTHS[now.month]} {now.year}"


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
