import os
import glob
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, EmailStr, Field
from playwright.sync_api import sync_playwright
from csv_parser import extract_appetizer_metrics

os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", "0")

TRAY_HOME = "https://hq.dine.tray.com"
MENU_MIX_URL = f"{TRAY_HOME}/tray/admin/reports?page=menuMix"
CENTRAL = ZoneInfo("America/Chicago")

app = FastAPI(title="App Attack Live Collector")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://app-attack-live.lumpsr.chatgpt.site"],
    allow_methods=["POST", "OPTIONS"],
    allow_headers=["Content-Type"],
)


def chromium_executable() -> str | None:
    candidates = [
        *glob.glob("/opt/render/.cache/ms-playwright/chromium-*/chrome-linux*/chrome"),
        *glob.glob("/opt/render/.cache/ms-playwright/chromium_headless_shell-*/chrome-linux*/headless_shell"),
        shutil.which("chromium"),
        shutil.which("chromium-browser"),
        shutil.which("google-chrome"),
    ]
    return next((path for path in candidates if path and os.path.isfile(path)), None)


def ensure_chromium() -> None:
    expected = os.path.join(
        os.path.dirname(__file__),
        ".venv/lib/python3.12/site-packages/playwright/driver/package/.local-browsers",
    )
    if glob.glob(os.path.join(expected, "chromium_headless_shell-*", "chrome-linux*", "headless_shell")):
        return
    subprocess.run([sys.executable, "-m", "playwright", "install", "chromium"], check=True)


ensure_chromium()


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
    page.click("text=Sites :")
    page.click("div:has-text('Sites :') + div, button:has-text('Sites'), .sites-dropdown-selector")
    page.wait_for_timeout(1000)

    # TRAY keeps prior site selections when the report page is reused. Clear
    # every selected site so each Menu Mix report is truly location-specific.
    checked_sites = page.locator("input[type='checkbox']:checked:visible")
    for index in range(checked_sites.count() - 1, -1, -1):
        checked_sites.nth(index).uncheck(force=True)
    page.wait_for_timeout(400)

    exact_label = f"IHOP #{store}"
    matches = page.get_by_text(exact_label, exact=True).filter(visible=True)
    if matches.count() == 0:
        search_boxes = page.locator(
            "input[type='text']:visible:not([id*='Date']):not([name*='date']):not([id*='ate']):not([id*='Check'])"
        )
        if search_boxes.count() > 0:
            search_boxes.first.fill(store)
        page.wait_for_timeout(1500)
        matches = page.get_by_text(exact_label, exact=True).filter(visible=True)
        if matches.count() == 0:
            raise ValueError(f"IHOP #{store} is not available to this TRAY account.")

    # Exact matching is required for three-digit locations. Without it,
    # IHOP #123 can also match IHOP #1234, #1235, and similar locations.
    matches.first.click()

    page.keyboard.press("Escape")
    page.wait_for_timeout(500)


def fetch_store(page, store: str, download_dir: str) -> dict[str, float | int]:
    page.goto(MENU_MIX_URL, wait_until="networkidle")
    run_report = page.locator("text='Run Report'").filter(visible=True).first
    run_report.wait_for(timeout=20000)
    select_store(page, store)
    run_report.click()
    csv_export = page.locator("text=CSV").filter(visible=True).first
    csv_export.wait_for(timeout=60000)
    with page.expect_download(timeout=60000) as info:
        csv_export.click()
    path = os.path.join(download_dir, f"menu-mix-{store}.csv")
    info.value.save_as(path)
    with open(path, encoding="utf-8-sig", newline="") as handle:
        return extract_appetizer_metrics(handle.read())


@app.get("/health")
def health():
    return {"status": "ok", "release": "exact-store-labels-1"}


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
                executable = chromium_executable()
                launch_options = {"headless": True, "args": ["--no-sandbox"]}
                if executable:
                    launch_options["executable_path"] = executable
                browser = playwright.chromium.launch(**launch_options)
                context = browser.new_context(accept_downloads=True)
                page = context.new_page()
                try:
                    login(page, request.email, request.password)
                    for store in clean_stores:
                        try:
                            metrics = fetch_store(page, store, temp_dir)
                            results.append({
                                "store": store,
                                "appetizerCount": metrics["count"],
                                "appetizerPercent": metrics["percent"],
                                "status": "ok",
                            })
                        except Exception as exc:
                            results.append({"store": store, "appetizerPercent": 0, "status": "error", "message": str(exc)})
                finally:
                    browser.close()
        except ValueError as exc:
            raise HTTPException(401, str(exc)) from exc
        except Exception as exc:
            message = str(exc)
            if "Executable doesn't exist" in message:
                message = "The report browser is temporarily unavailable. Please try again in a moment."
            raise HTTPException(502, message) from exc

    return {"updatedAt": datetime.now(CENTRAL).isoformat(), "results": results}
