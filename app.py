import os
import tempfile
from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, EmailStr, Field
from playwright.sync_api import sync_playwright
from csv_parser import extract_appetizer_percent

TRAY_HOME = "https://hq.dine.tray.com"
MENU_MIX_URL = f"{TRAY_HOME}/tray/admin/reports?page=menuMix"
CENTRAL = ZoneInfo("America/Chicago")

app = FastAPI(title="App Attack Live Collector")


class FetchRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=1)
    stores: list[str] = Field(min_length=1, max_length=40)


def visible(page, selector: str) -> bool:
    try:
        return page.locator(selector).first.is_visible(timeout=500)
    except Exception:
        return False


def login(page, email: str, password: str) -> None:
    page.goto(TRAY_HOME, wait_until="networkidle")
    page.locator("input[type='email'], input[placeholder*='Email'], input#username").first.fill(email)
    page.locator("input[type='password'], input[placeholder*='Password']").first.fill(password)
    page.locator("button[type='submit'], input[type='submit'], button:has-text('LOGIN'), button:has-text('Sign In')").first.click()
    page.wait_for_timeout(1200)
    page.wait_for_function(
        """() => !document.querySelector("input[type='password']") || location.pathname.includes('/tray/admin/')""",
        timeout=45000,
    )
    if visible(page, "text=Invalid") or visible(page, "text=incorrect"):
        raise ValueError("TRAY rejected the email or password.")


def select_store(page, store: str) -> None:
    site_box = page.locator("text=Sites :").locator("..").first
    try:
        site_box.locator("div, button, input").last.click()
    except Exception:
        page.locator("[class*='site'], [id*='site']").filter(visible=True).first.click()
    page.wait_for_timeout(700)
    search = page.locator("input[type='text']:visible").filter(
        has_not=page.locator("[id*='Date'], [name*='date']")
    )
    if search.count():
        search.last.fill(store)
        page.wait_for_timeout(700)
    matches = page.get_by_text(f"IHOP #{store}", exact=True)
    if matches.count() == 0:
        raise ValueError(f"IHOP #{store} is not available to this TRAY account.")
    matches.first.click()
    page.keyboard.press("Escape")
    page.wait_for_timeout(700)


def fetch_store(page, store: str, download_dir: str) -> float:
    page.goto(MENU_MIX_URL, wait_until="networkidle")
    page.get_by_text("Run Report", exact=True).wait_for(timeout=20000)
    select_store(page, store)
    page.get_by_text("Run Report", exact=True).click()
    page.get_by_text("CSV", exact=True).wait_for(timeout=60000)
    with page.expect_download(timeout=60000) as info:
        page.get_by_text("CSV", exact=True).click()
    path = os.path.join(download_dir, f"menu-mix-{store}.csv")
    info.value.save_as(path)
    with open(path, encoding="utf-8-sig", newline="") as handle:
        return extract_appetizer_percent(handle.read())


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/fetch-appetizers")
def fetch_appetizers(request: FetchRequest):
    clean_stores = list(dict.fromkeys("".join(ch for ch in store if ch.isdigit()) for store in request.stores))
    clean_stores = [store for store in clean_stores if store]
    if not clean_stores:
        raise HTTPException(400, "No valid store numbers were supplied.")

    results = []
    with tempfile.TemporaryDirectory() as temp_dir:
        try:
            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True, args=["--no-sandbox"])
                context = browser.new_context(accept_downloads=True)
                page = context.new_page()
                try:
                    login(page, request.email, request.password)
                    for store in clean_stores:
                        try:
                            value = fetch_store(page, store, temp_dir)
                            results.append({"store": store, "appetizerPercent": value, "status": "ok"})
                        except Exception as exc:
                            results.append({"store": store, "appetizerPercent": 0, "status": "error", "message": str(exc)})
                finally:
                    browser.close()
        except ValueError as exc:
            raise HTTPException(401, str(exc)) from exc
        except Exception as exc:
            raise HTTPException(502, f"TRAY collector could not complete the request: {exc}") from exc

    return {"updatedAt": datetime.now(CENTRAL).isoformat(), "results": results}
