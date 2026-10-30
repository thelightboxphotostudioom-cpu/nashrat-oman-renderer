import base64, sys
from jinja2 import Environment, FileSystemLoader
from playwright.sync_api import sync_playwright

ROOT = "/home/claude/nashrat-renderer"

with open(f"{ROOT}/assets/tajawal_base64.css") as f:
    tajawal_css = f.read()
with open(f"{ROOT}/assets/khanjar_base64.txt") as f:
    logo_b64 = f.read()
with open(f"{ROOT}/assets/khanjar_navy_base64.txt") as f:
    logo_navy_b64 = f.read()

env = Environment(loader=FileSystemLoader(f"{ROOT}/templates"))
tpl = env.get_template("post.html")

sample = dict(
    tajawal_css=tajawal_css,
    logo_b64=logo_b64,
    logo_navy_b64=logo_navy_b64,
    photo_url="file:///home/claude/nashrat-renderer/assets/real_test.jpg",
    category_emoji="🚢",
    category_label="عاجل",
    photo_tag_text="صورة حقيقية",
    ai_disclaimer=False,
    is_mourning=False,
    headline="استهداف ناقلة بنمية قبالة مسندم واجلاء 25 بحاراً بالتعاون بين البحرية والقوات الجوية",
    summary="نفذت البحرية السلطانية العمانية، بالتعاون مع مركز الأمن البحري، عملية إجلاء لـ25 فرداً من طاقم السفينة التجارية RITI بعد استهدافها واندلاع حريق على متنها.",
    source="Times of Oman",
    date_text="30 أكتوبر 2026",
)

html = tpl.render(**sample)
with open(f"{ROOT}/render_test.html", "w") as f:
    f.write(html)

with sync_playwright() as p:
    browser = p.chromium.launch()
    page = browser.new_page(viewport={"width": 1080, "height": 1080})
    page.goto(f"file://{ROOT}/render_test.html")
    page.wait_for_timeout(300)
    page.screenshot(path=f"{ROOT}/render_test_output.png")
    browser.close()

print("done")
