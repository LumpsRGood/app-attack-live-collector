import os
import csv
import glob
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, EmailStr, Field
from playwright.sync_api import sync_playwright
from csv_parser import extract_appetizer_metrics

os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", "0")

TRAY_HOME = "https://hq.dine.tray.com"
MENU_MIX_URL = f"{TRAY_HOME}/tray/admin/reports?page=menuMix"
LABOR_SUMMARY_URL = f"{TRAY_HOME}/tray/admin/reports?page=laborSummary"
CENTRAL = ZoneInfo("America/Chicago")

app = FastAPI(title="App Attack Live Collector")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://app-attack-live.lumpsr.chatgpt.site", "https://tracker-24-2-validity.lumpsr.chatgpt.site"],
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


class OvernightRequest(FetchRequest):
    period: str = "Last Week"
    groupBy: str = "Hour"


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


def select_store(page, store: str) -> str:
    sites_label = page.get_by_text("Sites :", exact=True).filter(visible=True).first
    sites_label.wait_for(state="visible", timeout=15000)
    sites_control = sites_label.locator("xpath=following-sibling::*[1]")
    sites_control.wait_for(state="visible", timeout=15000)
    sites_control.click()
    page.wait_for_timeout(1000)

    # TRAY keeps prior site selections when the report page is reused. Clear
    # every selected site so each Menu Mix report is truly location-specific.
    checked_sites = page.locator("input[type='checkbox']:checked:visible")
    for index in range(checked_sites.count() - 1, -1, -1):
        checked_sites.nth(index).uncheck(force=True)
    page.wait_for_timeout(400)

    candidates = [store]
    if len(store) == 3:
        candidates.append(store.zfill(4))

    def find_exact_match():
        for candidate in candidates:
            matches = page.get_by_text(f"IHOP #{candidate}", exact=True).filter(visible=True)
            if matches.count() > 0:
                return candidate, matches
        return None, None

    resolved_store, matches = find_exact_match()
    if matches is None:
        search_boxes = page.locator(
            "input[type='text']:visible:not([id*='Date']):not([name*='date']):not([id*='ate']):not([id*='Check'])"
        )
        if search_boxes.count() > 0:
            for candidate in candidates:
                search_boxes.first.fill(candidate)
                page.wait_for_timeout(1000)
                resolved_store, matches = find_exact_match()
                if matches is not None:
                    break
        if matches is None:
            raise ValueError(f"IHOP #{store} is not available to this TRAY account.")

    # Exact matching avoids collisions such as #123 and #1234. TRAY stores
    # some three-digit locations with a leading zero, such as #413 as #0413.
    matches.first.click()

    page.keyboard.press("Escape")
    page.wait_for_timeout(500)
    return resolved_store


def fetch_store(page, store: str, download_dir: str) -> tuple[dict[str, float | int], str]:
    page.goto(MENU_MIX_URL, wait_until="networkidle")
    run_report = page.locator("text='Run Report'").filter(visible=True).first
    run_report.wait_for(timeout=20000)
    resolved_store = select_store(page, store)
    run_report.click()
    csv_export = page.locator("text=CSV").filter(visible=True).first
    csv_export.wait_for(timeout=60000)
    with page.expect_download(timeout=60000) as info:
        csv_export.click()
    path = os.path.join(download_dir, f"menu-mix-{store}.csv")
    info.value.save_as(path)
    with open(path, encoding="utf-8-sig", newline="") as handle:
        return extract_appetizer_metrics(handle.read()), resolved_store



def select_option_by_label(page, label_text: str, option_text: str) -> None:
    label = page.get_by_text(label_text, exact=True).filter(visible=True)
    if label.count() > 0:
        container = label.first.locator("xpath=following-sibling::*[1]")
        native = container.locator("select")
        if native.count() > 0:
            native.first.select_option(label=option_text)
            return
        container.click()
    else:
        page.get_by_text(label_text).filter(visible=True).first.click()
    page.get_by_text(option_text, exact=True).filter(visible=True).first.click()
    page.keyboard.press("Escape")


def parse_money(value: str) -> float:
    cleaned = (value or "").replace("$", "").replace(",", "").strip()
    try:
        return float(cleaned or 0)
    except ValueError:
        return 0.0


def parse_hour(value: str) -> datetime | None:
    start = (value or "").split(" to ", 1)[0].strip()
    try:
        return datetime.strptime(start, "%Y-%m-%d %I:%M %p")
    except ValueError:
        return None


def summarize_overnight_csv(path: str, store: str) -> tuple[str, list[dict]]:
    with open(path, encoding="utf-8-sig", newline="") as handle:
        source = list(csv.DictReader(handle))
    dated = [(parse_hour(row.get("Date", "")), row) for row in source if row.get("Date") != "Total"]
    dated = [(stamp, row) for stamp, row in dated if stamp is not None]
    if not dated:
        raise ValueError(f"Labor Summary returned no hourly data for IHOP #{store}.")
    latest = max(stamp for stamp, _ in dated)
    week_ending = latest.date() - timedelta(days=(latest.weekday() - 6) % 7)
    targets = {"Friday": week_ending - timedelta(days=2), "Saturday": week_ending - timedelta(days=1)}
    rows = []
    for night, target in targets.items():
        selected = [row for stamp, row in dated if stamp.date() == target and 0 <= stamp.hour < 6]
        sales = sum(parse_money(row.get("Net Sales", "")) for row in selected)
        wages = sum(parse_money(row.get("Total Wages", "")) for row in selected)
        hours = sum(parse_money(row.get("Total Hours", "")) for row in selected)
        overtime = sum(parse_money(row.get("Overtime Wages", "")) for row in selected)
        rows.append({
            "store": store,
            "night": night,
            "overnightSales": round(sales, 2),
            "laborCost": round(wages, 2),
            "laborHours": round(hours, 2),
            "overtimeWages": round(overtime, 2),
        })
    return week_ending.isoformat(), rows


def fetch_labor_summary(page, store: str, download_dir: str) -> tuple[str, list[dict], str]:
    page.goto(LABOR_SUMMARY_URL, wait_until="networkidle")
    page.get_by_text("Run Report", exact=True).filter(visible=True).first.wait_for(timeout=20000)
    select_option_by_label(page, "Period :", "Last Week")
    select_option_by_label(page, "Group By :", "Hour")
    resolved_store = select_store(page, store)
    page.get_by_text("Run Report", exact=True).filter(visible=True).first.click()
    csv_export = page.get_by_text("CSV", exact=True).filter(visible=True).first
    csv_export.wait_for(timeout=60000)
    with page.expect_download(timeout=60000) as info:
        csv_export.click()
    path = os.path.join(download_dir, f"labor-summary-{store}.csv")
    info.value.save_as(path)
    week_ending, rows = summarize_overnight_csv(path, resolved_store)
    return week_ending, rows, resolved_store


@app.post("/fetch-overnight-performance")
def fetch_overnight_performance(request: OvernightRequest):
    clean_stores = list(dict.fromkeys("".join(ch for ch in store if ch.isdigit()) for store in request.stores))
    clean_stores = [store for store in clean_stores if store]
    if not clean_stores:
        raise HTTPException(400, "No valid store numbers were supplied.")

    rows = []
    week_ending = None
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
                        store_week_ending, store_rows, _ = fetch_labor_summary(page, store, temp_dir)
                        if week_ending and week_ending != store_week_ending:
                            raise ValueError("TRAY returned different Last Week periods across locations.")
                        week_ending = store_week_ending
                        rows.extend(store_rows)
                finally:
                    browser.close()
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        except Exception as exc:
            raise HTTPException(502, str(exc)) from exc
    return {"weekEnding": week_ending, "rows": rows}


@app.get("/health")
def health():
    return {"status": "ok", "release": "visible-sites-control-1"}


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
                            metrics, resolved_store = fetch_store(page, store, temp_dir)
                            results.append({
                                "store": resolved_store,
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
