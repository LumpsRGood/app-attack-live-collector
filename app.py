import base64
import csv
import glob
import io
import os
import shutil
import subprocess
import sys
import tempfile
import time
import threading
import uuid
from datetime import date, datetime, timedelta
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
ORDERS_URL = f"{TRAY_HOME}/tray/admin/reports?page=ordersListNew"
CHECKS_URL = f"{TRAY_HOME}/tray/admin/reports?page=closeTabs"
CENTRAL = ZoneInfo("America/Chicago")

app = FastAPI(title="App Attack Live Collector")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["https://app-attack-live.lumpsr.chatgpt.site", "https://tracker-24-2-validity.lumpsr.chatgpt.site", "https://isitworth242.lumpsr.chatgpt.site", "https://peachtree-performance.lumpsr.chatgpt.site"],
    allow_methods=["GET", "POST", "OPTIONS"],
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
    # Render's native build installs Chromium in /opt/render/.cache. Reuse it
    # instead of downloading a second 172 MB browser during application startup.
    if chromium_executable():
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


class DailyReportsRequest(FetchRequest):
    business_date: date = Field(alias="businessDate")


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
        if native.count() > 0 and native.first.is_visible():
            native.first.select_option(label=option_text)
            return
        container.click()
    else:
        page.get_by_text(label_text).filter(visible=True).first.click()
    page.get_by_text(option_text, exact=True).filter(visible=True).first.click()
    page.keyboard.press("Escape")


def clear_and_fill(page, selector: str, value: str) -> None:
    locator = page.locator(selector).first
    locator.click()
    locator.fill("")
    locator.fill(value)


def goto_report(page, url: str) -> None:
    page.goto(url, wait_until="domcontentloaded", timeout=60000)
    try:
        page.wait_for_load_state("networkidle", timeout=10000)
    except Exception:
        pass
    page.locator("text='Run Report'").filter(visible=True).first.wait_for(
        state="visible", timeout=20000
    )


def wait_for_tray(page, timeout: int = 180000) -> None:
    busy_locators = [
        page.locator("text=/please wait/i"),
        page.locator("text=/loading/i"),
        page.locator(".blockUI:visible"),
        page.locator(".loading:visible"),
        page.locator(".spinner:visible"),
    ]
    deadline = time.time() + timeout / 1000
    while time.time() < deadline:
        if not any(
            locator.count() > 0 and locator.first.is_visible()
            for locator in busy_locators
        ):
            return
        page.wait_for_timeout(1000)


def configure_daily_report(page, report_type: str, store: str, business_date: date) -> str:
    date_text = business_date.strftime("%m/%d/%Y")
    if report_type == "checks":
        goto_report(page, CHECKS_URL)
        select_option_by_label(page, "Period :", "Today")
        clear_and_fill(
            page,
            "input:visible[id*='Start'], input:visible[name*='start'], input:visible[placeholder*='Start']",
            date_text,
        )
        clear_and_fill(
            page,
            "input:visible[id*='End'], input:visible[name*='end'], input:visible[placeholder*='End']",
            date_text,
        )
        resolved_store = select_store(page, store)
        select_option_by_label(page, "Tender Type :", "Card")
        return resolved_store

    goto_report(page, ORDERS_URL)
    clear_and_fill(page, "#datepicker", date_text)
    resolved_store = select_store(page, store)
    select_option_by_label(page, "Service :", "Eat In")
    return resolved_store


def orders_csv(page, timeout: int = 300000) -> bytes:
    page.locator("text='Run Report'").filter(visible=True).first.click()
    try:
        page.wait_for_load_state("networkidle", timeout=30000)
    except Exception:
        pass
    page.wait_for_timeout(2500)
    wait_for_tray(page, timeout)
    page.wait_for_selector("#ordersReportTable tbody tr", timeout=timeout)
    rows = page.locator("#ordersReportTable tbody tr").evaluate_all(
        """(trs) => trs
            .map((tr) => Array.from(tr.querySelectorAll('td')).map((td) => td.innerText.replace(/\\s+/g, ' ').trim()))
            .filter((row) => row.length)"""
    )
    if not rows:
        raise ValueError("Orders report returned no rows.")
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow([
        "Time", "ID Site", "Service Destination", "Routing",
        "Device Orders Report", "Items", "Staff Customer",
        "Check ID Check Number", "Base (Including Disc.)", "Tax",
        "Fees", "Total (Excluding Tip)", "Print Status", "Action",
    ])
    writer.writerows(rows)
    return output.getvalue().encode("utf-8")


def checks_csv(page, download_dir: str, store: str, timeout: int = 180000) -> bytes:
    page.locator("text='Run Report'").filter(visible=True).first.click()
    wait_for_tray(page, timeout)
    page.wait_for_function(
        """() => Array.from(document.querySelectorAll('span, a, button, [role="button"]')).some((node) => {
            const style = window.getComputedStyle(node);
            const rect = node.getBoundingClientRect();
            return node.textContent.trim() === 'CSV'
                && style.display !== 'none'
                && style.visibility !== 'hidden'
                && rect.width > 0
                && rect.height > 0;
        })""",
        timeout=timeout,
    )
    with page.expect_download(timeout=timeout) as info:
        page.evaluate(
            """() => {
                const nodes = Array.from(document.querySelectorAll('span, a, button, [role="button"]'));
                const node = nodes.find((candidate) => {
                    const style = window.getComputedStyle(candidate);
                    const rect = candidate.getBoundingClientRect();
                    return candidate.textContent.trim() === 'CSV'
                        && style.display !== 'none'
                        && style.visibility !== 'hidden'
                        && rect.width > 0
                        && rect.height > 0;
                });
                if (!node) throw new Error('CSV export control disappeared before it could be clicked.');
                (node.closest('a, button, [role="button"], [onclick]') || node).click();
            }"""
        )
    path = os.path.join(download_dir, f"checks-{store}.csv")
    info.value.save_as(path)
    with open(path, "rb") as handle:
        return handle.read()


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
    # Operational Friday runs into Saturday morning; operational Saturday
    # runs into Sunday morning (the official week-ending date).
    targets = {"Friday": week_ending - timedelta(days=1), "Saturday": week_ending}
    rows = []
    for night, target in targets.items():
        by_hour = {stamp.hour: row for stamp, row in dated if stamp.date() == target and 0 <= stamp.hour < 6}
        hourly = []
        for hour in range(6):
            row = by_hour.get(hour, {})
            hourly.append({
                "hour": hour,
                "netSales": round(parse_money(row.get("Net Sales", "")), 2),
                "totalWages": round(parse_money(row.get("Total Wages", "")), 2),
                "laborHours": round(parse_money(row.get("Total Hours", "")), 2),
                "overtimeWages": round(parse_money(row.get("Overtime Wages", "")), 2),
            })
        sales = sum(item["netSales"] for item in hourly)
        wages = sum(item["totalWages"] for item in hourly)
        hours = sum(item["laborHours"] for item in hourly)
        overtime = sum(item["overtimeWages"] for item in hourly)
        rows.append({
            "store": store,
            "night": night,
            "calendarDate": target.isoformat(),
            "overnightSales": round(sales, 2),
            "laborCost": round(wages, 2),
            "laborHours": round(hours, 2),
            "overtimeWages": round(overtime, 2),
            "hourly": hourly,
        })
    return week_ending.isoformat(), rows


def fetch_labor_summary(page, store: str, download_dir: str) -> tuple[str, list[dict], str]:
    page.goto(LABOR_SUMMARY_URL, wait_until="domcontentloaded")
    run_report = page.locator("text='Run Report'").filter(visible=True).first
    run_report.wait_for(state="visible", timeout=30000)
    select_option_by_label(page, "Period :", "Last Week")
    select_option_by_label(page, "Group By :", "Hour")
    resolved_store = select_store(page, store)
    run_report.click()
    # TRAY re-renders the export toolbar after the report finishes. A
    # text-only Playwright locator can keep waiting on an obsolete CSV span
    # even while the replacement is visibly on screen. Resolve and click the
    # current visible export control in the DOM instead.
    page.wait_for_function(
        """() => Array.from(document.querySelectorAll('span, a, button, [role="button"]')).some((node) => {
            const style = window.getComputedStyle(node);
            const rect = node.getBoundingClientRect();
            return node.textContent.trim() === 'CSV'
                && style.display !== 'none'
                && style.visibility !== 'hidden'
                && rect.width > 0
                && rect.height > 0;
        })""",
        timeout=60000,
    )
    with page.expect_download(timeout=60000) as info:
        page.evaluate(
            """() => {
                const nodes = Array.from(document.querySelectorAll('span, a, button, [role="button"]'));
                const node = nodes.find((candidate) => {
                    const style = window.getComputedStyle(candidate);
                    const rect = candidate.getBoundingClientRect();
                    return candidate.textContent.trim() === 'CSV'
                        && style.display !== 'none'
                        && style.visibility !== 'hidden'
                        && rect.width > 0
                        && rect.height > 0;
                });
                if (!node) throw new Error('CSV export control disappeared before it could be clicked.');
                (node.closest('a, button, [role="button"], [onclick]') || node).click();
            }"""
        )
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
                launch_options = {"headless": True, "args": ["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu", "--no-zygote", "--single-process", "--js-flags=--max-old-space-size=96"]}
                if executable:
                    launch_options["executable_path"] = executable
                browser = playwright.chromium.launch(**launch_options)
                context = browser.new_context(accept_downloads=True)
                context.route("**/*", lambda route: route.abort() if route.request.resource_type in ("image", "media", "font") else route.continue_())
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
    return {"status": "ok", "release": "daily-jobs-5"}


@app.post("/fetch-daily-reports")
def fetch_daily_reports(request: DailyReportsRequest):
    clean_stores = list(dict.fromkeys("".join(ch for ch in store if ch.isdigit()) for store in request.stores))
    clean_stores = [store for store in clean_stores if store]
    if not clean_stores:
        raise HTTPException(400, "No valid store numbers were supplied.")

    files = []
    with tempfile.TemporaryDirectory() as temp_dir:
        try:
            with sync_playwright() as playwright:
                executable = chromium_executable()
                launch_options = {"headless": True, "args": ["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu", "--no-zygote", "--js-flags=--max-old-space-size=192"]}
                if executable:
                    launch_options["executable_path"] = executable
                browser = playwright.chromium.launch(**launch_options)
                context = browser.new_context(accept_downloads=True)
                context.route("**/*", lambda route: route.abort() if route.request.resource_type in ("image", "media", "font") else route.continue_())
                page = context.new_page()
                try:
                    login(page, request.email, request.password)
                    date_part = request.business_date.strftime("%Y%m%d")
                    for store in clean_stores:
                        for report_type in ("orders", "checks"):
                            resolved_store = configure_daily_report(
                                page, report_type, store, request.business_date
                            )
                            if report_type == "orders":
                                content = orders_csv(page)
                            else:
                                content = checks_csv(page, temp_dir, resolved_store)
                            files.append({
                                "store": resolved_store,
                                "reportType": report_type,
                                "filename": f"tray_{report_type}_{resolved_store}_{date_part}.csv",
                                "contentBase64": base64.b64encode(content).decode("ascii"),
                            })
                finally:
                    browser.close()
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        except Exception as exc:
            raise HTTPException(502, str(exc)) from exc

    return {"businessDate": request.business_date.isoformat(), "files": files}


JOBS: dict[str, dict] = {}
JOBS_LOCK = threading.Lock()


def _run_daily_job(job_id: str, request: DailyReportsRequest) -> None:
    for attempt in range(2):
        try:
            result = fetch_daily_reports(request)
            with JOBS_LOCK:
                JOBS[job_id] = {"status": "succeeded", "result": result}
            return
        except HTTPException as exc:
            message = str(exc.detail)
            browser_closed = "page, context or browser has been closed" in message.lower()
            if attempt == 0 and browser_closed:
                time.sleep(2)
                continue
            with JOBS_LOCK:
                JOBS[job_id] = {"status": "failed", "error": message}
            return
        except Exception as exc:
            message = str(exc)
            browser_closed = "page, context or browser has been closed" in message.lower()
            if attempt == 0 and browser_closed:
                time.sleep(2)
                continue
            with JOBS_LOCK:
                JOBS[job_id] = {"status": "failed", "error": message}
            return


@app.post("/start-daily-reports")
def start_daily_reports(request: DailyReportsRequest):
    job_id = uuid.uuid4().hex
    with JOBS_LOCK:
        JOBS[job_id] = {"status": "running"}
    threading.Thread(target=_run_daily_job, args=(job_id, request), daemon=True).start()
    return {"jobId": job_id, "status": "running"}


@app.get("/jobs/{job_id}")
def get_job(job_id: str):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "Collection job not found.")
    return job


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
                launch_options = {"headless": True, "args": ["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu", "--no-zygote", "--single-process", "--js-flags=--max-old-space-size=96"]}
                if executable:
                    launch_options["executable_path"] = executable
                browser = playwright.chromium.launch(**launch_options)
                context = browser.new_context(accept_downloads=True)
                context.route("**/*", lambda route: route.abort() if route.request.resource_type in ("image", "media", "font") else route.continue_())
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
