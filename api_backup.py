
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse

import psycopg
import requests
from dotenv import load_dotenv
from playwright.sync_api import (
    sync_playwright,
    TimeoutError as PlaywrightTimeoutError,
)

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

DB_CONFIG = {
    "host": os.getenv("DB_HOST", "localhost"),
    "dbname": os.getenv("DB_NAME", "smart_qa"),
    "user": os.getenv("DB_USER", "postgres"),
    "password": os.getenv("DB_PASSWORD", ""),
    "port": int(os.getenv("DB_PORT", "5432")),
    "connect_timeout": 5,
}

MAX_LINKS = int(os.getenv("MAX_LINKS", "80"))
MAX_IMAGES = int(os.getenv("MAX_IMAGES", "80"))
REQUEST_TIMEOUT = float(os.getenv("REQUEST_TIMEOUT", "6"))
NAVIGATION_TIMEOUT_MS = int(
    os.getenv("NAVIGATION_TIMEOUT_MS", "45000")
)


def get_connection():
    if not DB_CONFIG["password"]:
        raise RuntimeError("DB_PASSWORD is missing from .env")

    return psycopg.connect(**DB_CONFIG)


def ensure_tables():
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS scans (
                    id BIGSERIAL PRIMARY KEY,
                    website_url TEXT,
                    status_code INTEGER,
                    page_title TEXT,
                    scan_time TIMESTAMPTZ DEFAULT NOW()
                )
            """)

            cur.execute("""
                ALTER TABLE scans
                ADD COLUMN IF NOT EXISTS status TEXT DEFAULT 'completed'
            """)

            cur.execute("""
                ALTER TABLE scans
                ADD COLUMN IF NOT EXISTS error_message TEXT
            """)

            cur.execute("""
                CREATE TABLE IF NOT EXISTS issues (
                    id BIGSERIAL PRIMARY KEY,
                    scan_id BIGINT,
                    issue_type TEXT DEFAULT 'QA_ISSUE',
                    issue_url TEXT,
                    status_code INTEGER,
                    message TEXT,
                    severity TEXT DEFAULT 'LOW',
                    created_at TIMESTAMPTZ DEFAULT NOW()
                )
            """)

            cur.execute("""
                CREATE INDEX IF NOT EXISTS idx_issues_scan_id
                ON issues(scan_id)
            """)


def save_scan(
    url,
    status_code,
    page_title,
    status="completed",
    error_message=None,
):
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO scans (
                    website_url,
                    status_code,
                    page_title,
                    status,
                    error_message
                )
                VALUES (%s, %s, %s, %s, %s)
                RETURNING id
            """, (
                url,
                status_code,
                page_title,
                status,
                error_message,
            ))

            return cur.fetchone()[0]


def save_issue(
    scan_id,
    issue_type,
    issue_url,
    status_code,
    message,
    severity,
):
    if not scan_id:
        return

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO issues (
                    scan_id,
                    issue_type,
                    issue_url,
                    status_code,
                    message,
                    severity
                )
                VALUES (%s, %s, %s, %s, %s, %s)
            """, (
                scan_id,
                issue_type,
                issue_url,
                status_code,
                str(message)[:2000],
                severity,
            ))


def add_issue(
    issues,
    scan_id,
    issue_type,
    issue_url,
    status_code,
    message,
    severity="LOW",
):
    issue = {
        "issue_type": issue_type or "QA_ISSUE",
        "issue_url": issue_url,
        "status_code": status_code,
        "message": str(message)[:2000],
        "severity": severity,
    }

    issues.append(issue)

    try:
        save_issue(
            scan_id,
            issue["issue_type"],
            issue["issue_url"],
            issue["status_code"],
            issue["message"],
            issue["severity"],
        )
    except Exception as exc:
        print(f"[database] Could not save issue: {exc}")


def normalize_url(url):
    url = (url or "").strip()

    if not url:
        raise ValueError("Enter a website URL.")

    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    parsed = urlparse(url)

    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError("Enter a valid HTTP or HTTPS URL.")

    return url


def check_url(session, url):
    """Check URLs using requests, not Playwright's sync request API."""
    try:
        response = session.get(
            url,
            timeout=REQUEST_TIMEOUT,
            allow_redirects=True,
            stream=True,
        )

        status = response.status_code
        response.close()

        if 200 <= status < 400:
            return "working", status, f"HTTP {status}"

        if status in (404, 410):
            return "broken", status, f"HTTP {status}"

        return (
            "needs_review",
            status,
            f"HTTP {status}; verify manually",
        )

    except requests.exceptions.Timeout:
        return "needs_review", None, "Request timed out; unable to verify"

    except requests.exceptions.SSLError:
        return "needs_review", None, "TLS/SSL error; unable to verify"

    except requests.RequestException as exc:
        return (
            "needs_review",
            None,
            f"Request failed; unable to verify: {str(exc)[:250]}",
        )


def check_links(session, base_url, hrefs, scan_id, issues):
    total = len(hrefs)
    candidates = []
    seen = set()
    skipped = 0

    for href in hrefs:
        href = (href or "").strip()

        if not href or href.startswith("#") or href.lower().startswith(
            ("mailto:", "tel:", "javascript:", "data:", "blob:")
        ):
            skipped += 1
            continue

        full_url = urljoin(base_url, href)

        if urlparse(full_url).scheme not in ("http", "https"):
            skipped += 1
            continue

        if full_url not in seen:
            seen.add(full_url)
            candidates.append(full_url)

    limited = candidates[:MAX_LINKS]
    skipped += max(0, len(candidates) - len(limited))

    report = {
        "total": total,
        "unique": len(candidates),
        "working": 0,
        "broken": 0,
        "needs_review": 0,
        "skipped": skipped,
        "not_checked_limit": max(0, len(candidates) - len(limited)),
        "checked": 0,
        "internal": 0,
        "external": 0,
        "items": [],
    }

    base_host = urlparse(base_url).netloc

    for link_url in limited:
        category = (
            "internal"
            if urlparse(link_url).netloc == base_host
            else "external"
        )

        result, status, reason = check_url(session, link_url)

        report[result] += 1
        report["checked"] += 1

        if category == "internal":
            report["internal"] += 1
        else:
            report["external"] += 1

        report["items"].append({
            "url": link_url,
            "category": category,
            "result": result,
            "status_code": status,
            "reason": reason,
        })

        if result == "broken":
            add_issue(
                issues, scan_id, "BROKEN_LINK",
                link_url, status, reason, "HIGH",
            )
        elif result == "needs_review":
            add_issue(
                issues, scan_id, "NEEDS_REVIEW",
                link_url, status, reason, "LOW",
            )

    report["skipped_protocol"] = skipped
    return report


def check_images(session, base_url, sources, scan_id, issues):
    candidates = []
    seen = set()
    skipped = 0

    for src in sources:
        src = (src or "").strip()

        if not src or src.lower().startswith(("data:", "blob:")):
            skipped += 1
            continue

        full_url = urljoin(base_url, src)

        if urlparse(full_url).scheme not in ("http", "https"):
            skipped += 1
            continue

        if full_url not in seen:
            seen.add(full_url)
            candidates.append(full_url)

    limited = candidates[:MAX_IMAGES]
    skipped += max(0, len(candidates) - len(limited))

    report = {
        "total": len(sources),
        "unique": len(candidates),
        "working": 0,
        "broken": 0,
        "needs_review": 0,
        "skipped": skipped,
        "not_checked_limit": max(0, len(candidates) - len(limited)),
        "checked": 0,
        "items": [],
    }

    for image_url in limited:
        result, status, reason = check_url(session, image_url)

        report[result] += 1
        report["checked"] += 1

        report["items"].append({
            "url": image_url,
            "result": result,
            "status_code": status,
            "reason": reason,
        })

        if result == "broken":
            add_issue(
                issues, scan_id, "BROKEN_IMAGE",
                image_url, status, reason, "MEDIUM",
            )
        elif result == "needs_review":
            add_issue(
                issues, scan_id, "NEEDS_REVIEW",
                image_url, status, reason, "LOW",
            )

    return report


def analyze_performance(page, navigation_ms):
    try:
        data = page.evaluate("""
            () => {
                const resources = performance.getEntriesByType("resource");
                let images = 0, javascript = 0, css = 0;

                for (const r of resources) {
                    const name = r.name.toLowerCase();

                    if (r.initiatorType === "img" ||
                        /\\.(png|jpe?g|gif|webp|svg|ico)(\\?|$)/i.test(name)) {
                        images++;
                    } else if (r.initiatorType === "script" ||
                        /\\.js(\\?|$)/i.test(name)) {
                        javascript++;
                    } else if (r.initiatorType === "css" ||
                        r.initiatorType === "link" ||
                        /\\.css(\\?|$)/i.test(name)) {
                        css++;
                    }
                }

                const nav = performance.getEntriesByType("navigation")[0];
                const load = nav && nav.loadEventEnd > 0
                    ? Math.round(nav.loadEventEnd - nav.startTime) : 0;
                const ttfb = nav
                    ? Math.round(nav.responseStart - nav.requestStart) : 0;

                return {
                    load_time_ms: load,
                    ttfb_ms: ttfb,
                    total_resources: resources.length,
                    images,
                    javascript,
                    css,
                    measurement_complete: Boolean(nav && nav.loadEventEnd > 0)
                };
            }
        """)

        data["navigation_time_ms"] = round(navigation_ms)

        if not data["load_time_ms"]:
            data["load_time_ms"] = round(navigation_ms)
            data["measurement_note"] = (
                "Navigation time used because browser load timing was unavailable."
            )

        return data

    except Exception as exc:
        return {
            "load_time_ms": round(navigation_ms),
            "ttfb_ms": 0,
            "total_resources": 0,
            "images": 0,
            "javascript": 0,
            "css": 0,
            "measurement_complete": False,
            "measurement_note": str(exc)[:250],
            "navigation_time_ms": round(navigation_ms),
        }


def calculate_health(links, images, javascript):
    score = 100
    deductions = []

    checks = [
        ("broken links", links.get("broken", 0), 5, 35),
        ("broken images", images.get("broken", 0), 3, 20),
        (
            "JavaScript errors",
            javascript.get("console_errors", 0)
            + javascript.get("page_errors", 0),
            5,
            25,
        ),
    ]

    for name, count, points_each, cap in checks:
        points = min(cap, count * points_each)

        if points:
            score -= points
            deductions.append({
                "reason": name,
                "points": points,
            })

    score = max(0, score)
    grade = (
        "A" if score >= 90 else
        "B" if score >= 80 else
        "C" if score >= 70 else
        "D" if score >= 60 else "F"
    )

    return {
        "score": score,
        "grade": grade,
        "deductions": deductions,
        "note": "Unverified links and skipped items do not reduce the score.",
    }


def run_qa_scan(raw_url):
    url = normalize_url(raw_url)

    issues = []
    console_errors = []
    page_errors = []
    failed_requests = []
    status_code = None
    page_title = ""
    navigation_timed_out = False
    started = time.perf_counter()

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context()
        page = context.new_page()
        page.set_default_timeout(8000)

        def on_console(message):
            if message.type == "error":
                message_text = message.text

                if not message_text.startswith("Failed to load resource:"):
                    console_errors.append(message_text[:500])

        def on_page_error(error):
            page_errors.append(str(error)[:500])

        def on_request_failed(request):
            failed_requests.append({
                "url": request.url[:300],
                "error": str(request.failure or "unknown")[:200],
            })

        page.on("console", on_console)
        page.on("pageerror", on_page_error)
        page.on("requestfailed", on_request_failed)

        try:
            response = page.goto(
                url,
                wait_until="domcontentloaded",
                timeout=NAVIGATION_TIMEOUT_MS,
            )
            status_code = response.status if response else None

        except PlaywrightTimeoutError:
            navigation_timed_out = True

        except Exception as exc:
            browser.close()
            raise RuntimeError(f"Could not open {url}: {exc}") from exc

        navigation_ms = (time.perf_counter() - started) * 1000

        try:
            page.wait_for_load_state("load", timeout=10000)
        except PlaywrightTimeoutError:
            pass

        try:
            page_title = page.title()[:500]
        except Exception:
            page_title = ""

        # Extract browser-owned DOM information on the same thread.
        try:
            hrefs = page.locator("a[href]").evaluate_all(
                "(els) => els.map(e => e.getAttribute('href'))"
            )
        except Exception:
            hrefs = []

        try:
            image_sources = page.locator("img").evaluate_all(
                "(els) => els.map(e => e.currentSrc || e.getAttribute('src'))"
            )
        except Exception:
            image_sources = []

        try:
            page.screenshot(
                path=str(BASE_DIR / "homepage.png"),
                full_page=True,
                timeout=10000,
            )
        except Exception as exc:
            print(f"[screenshot] {exc}")

        performance_report = analyze_performance(page, navigation_ms)
        browser.close()

    scan_id = save_scan(url, status_code, page_title)

    session = requests.Session()
    session.headers.update({
        "User-Agent": "SmartWebsiteQAAgent/3.0"
    })

    try:
        links_report = check_links(
            session, url, hrefs, scan_id, issues
        )
        images_report = check_images(
            session, url, image_sources, scan_id, issues
        )
    finally:
        session.close()

    for error in console_errors:
        add_issue(
            issues, scan_id, "CONSOLE_ERROR",
            url, None, error, "MEDIUM",
        )

    for error in page_errors:
        add_issue(
            issues, scan_id, "JAVASCRIPT_RUNTIME_ERROR",
            url, None, error, "HIGH",
        )

    javascript_report = {
        "console_errors": len(console_errors),
        "page_errors": len(page_errors),
        "errors": console_errors + [
            f"Runtime: {error}" for error in page_errors
        ],
        "failed_requests": failed_requests[:30],
    }

    health = calculate_health(
        links_report,
        images_report,
        javascript_report,
    )

    report = {
        "scan": {
            "scan_id": scan_id,
            "url": url,
            "status_code": status_code,
            "page_title": page_title,
            "scan_time": datetime.now(timezone.utc).isoformat(),
            "navigation_timed_out": navigation_timed_out,
            "navigation_time_ms": round(navigation_ms),
        },
        "links": links_report,
        "images": images_report,
        "javascript": javascript_report,
        "performance": performance_report,
        "health": health,
        "issues": issues,
    }

    (BASE_DIR / "report.json").write_text(
        json.dumps(report, indent=2),
        encoding="utf-8",
    )

    return report


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument(
        "url",
        nargs="?",
        default="https://example.com",
    )
    args = parser.parse_args()

    print(json.dumps(run_qa_scan(args.url), indent=2))
