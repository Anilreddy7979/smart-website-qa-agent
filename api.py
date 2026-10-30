"""
api.py – Scanner engine and database functions for QA Sentinel.

DB_CONFIG and get_connection() are defined here and imported by qa_test.py
so credentials and connection logic live in exactly one place.
"""

import ipaddress
import json
import os
import socket
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse

import psycopg
import requests
from dotenv import load_dotenv
from playwright.sync_api import (
    TimeoutError as PlaywrightTimeoutError,
    sync_playwright,
)

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

# ──────────────────────────────────────────────────────────────────────────────
# Database configuration
# Defined here ONCE.  qa_test.py imports get_connection() from this module.
# ──────────────────────────────────────────────────────────────────────────────

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
NAVIGATION_TIMEOUT_MS = int(os.getenv("NAVIGATION_TIMEOUT_MS", "45000"))


# ──────────────────────────────────────────────────────────────────────────────
# SSRF protection
# Prevents the scanner from probing internal / private network addresses.
# ──────────────────────────────────────────────────────────────────────────────

_BLOCKED_HOSTNAMES = frozenset({
    "localhost",
    "localhost.localdomain",
    "ip6-localhost",
    "ip6-loopback",
})


def _is_private_ip(address: str) -> bool:
    """Return True if *address* is private, loopback, link-local, or unspecified."""
    try:
        ip = ipaddress.ip_address(address)
        return (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_unspecified
        )
    except ValueError:
        return False


def _is_ssrf_target(hostname: str) -> bool:
    """
    Return True if *hostname* is (or resolves to) a private/internal address.
    Blocks:
      - "localhost" and known loopback names
      - Literal private IPv4/IPv6 addresses (RFC-1918, loopback, link-local)
      - Hostnames that DNS-resolve to any of the above
    """
    hostname = hostname.lower().strip()

    if hostname in _BLOCKED_HOSTNAMES:
        return True

    # Hostname is a literal IP address
    if _is_private_ip(hostname):
        return True

    # Attempt DNS resolution and check every returned address
    try:
        results = socket.getaddrinfo(hostname, None, socket.AF_UNSPEC)
        for result in results:
            if _is_private_ip(result[4][0]):
                return True
    except (socket.gaierror, OSError):
        # Cannot resolve – let the scanner proceed; it will fail on its own
        pass

    return False


# ──────────────────────────────────────────────────────────────────────────────
# Database helpers
# ──────────────────────────────────────────────────────────────────────────────

def get_connection():
    if not DB_CONFIG["password"]:
        raise RuntimeError("DB_PASSWORD is missing from .env")
    return psycopg.connect(**DB_CONFIG)


def ensure_tables():
    """Create tables and safely add any missing columns."""
    with get_connection() as conn:
        with conn.cursor() as cur:

            # ── scans ──────────────────────────────────────────────────────
            cur.execute("""
                CREATE TABLE IF NOT EXISTS scans (
                    id          BIGSERIAL PRIMARY KEY,
                    website_url TEXT        NOT NULL,
                    status_code INTEGER,
                    page_title  TEXT,
                    health_score INTEGER,
                    status      TEXT        DEFAULT 'completed',
                    error_message TEXT,
                    report      JSONB,
                    scan_time   TIMESTAMPTZ DEFAULT NOW(),
                    finished_at TIMESTAMPTZ
                )
            """)

            # Safe column additions for pre-existing tables
            for col, defn in [
                ("status",        "TEXT DEFAULT 'completed'"),
                ("error_message", "TEXT"),
                ("health_score",  "INTEGER"),
                ("report",        "JSONB"),
                ("finished_at",   "TIMESTAMPTZ"),
            ]:
                cur.execute(
                    f"ALTER TABLE scans ADD COLUMN IF NOT EXISTS {col} {defn}"
                )

            # ── issues ─────────────────────────────────────────────────────
            cur.execute("""
                CREATE TABLE IF NOT EXISTS issues (
                    id          BIGSERIAL PRIMARY KEY,
                    scan_id     BIGINT      REFERENCES scans(id) ON DELETE CASCADE,
                    issue_type  TEXT        NOT NULL DEFAULT 'QA_ISSUE',
                    issue_url   TEXT,
                    status_code INTEGER,
                    message     TEXT,
                    severity    TEXT        DEFAULT 'LOW',
                    recommendation TEXT,
                    created_at  TIMESTAMPTZ DEFAULT NOW()
                )
            """)

            # Safe column addition for pre-existing issues table
            cur.execute("""
                ALTER TABLE issues
                ADD COLUMN IF NOT EXISTS recommendation TEXT
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
    """Insert a new scan row and return its id."""
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO scans (
                    website_url, status_code, page_title,
                    status, error_message
                )
                VALUES (%s, %s, %s, %s, %s)
                RETURNING id
            """, (url, status_code, page_title, status, error_message))
            return cur.fetchone()[0]


def update_scan_health(scan_id, health_score, report_json=None):
    """
    After the scan is complete, persist the health score and the full report
    JSON to the database and mark the finish time.
    """
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE scans
                SET health_score = %s,
                    report       = %s,
                    finished_at  = NOW()
                WHERE id = %s
            """, (
                health_score,
                json.dumps(report_json, default=str) if report_json else None,
                scan_id,
            ))


def _save_issue_to_db(
    scan_id, issue_type, issue_url,
    status_code, message, severity, recommendation,
):
    if not scan_id:
        return
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO issues (
                    scan_id, issue_type, issue_url,
                    status_code, message, severity, recommendation
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s)
            """, (
                scan_id,
                issue_type,
                issue_url,
                status_code,
                str(message)[:2000],
                severity,
                recommendation,
            ))


# ──────────────────────────────────────────────────────────────────────────────
# Recommendations
# ──────────────────────────────────────────────────────────────────────────────

_RECOMMENDATIONS = {
    "BROKEN_LINK": (
        "The link returns HTTP 404 or 410 (page not found). "
        "Update the URL to a working destination or remove the link entirely."
    ),
    "BROKEN_IMAGE": (
        "The image resource cannot be loaded. "
        "Check the file path, re-upload the image, or remove the broken reference."
    ),
    "CONSOLE_ERROR": (
        "A JavaScript console error was detected in the browser. "
        "Open browser DevTools (F12 → Console tab) on the page and fix the reported error."
    ),
    "JAVASCRIPT_RUNTIME_ERROR": (
        "A JavaScript runtime exception was thrown. "
        "Open DevTools, reproduce the error, then fix the exception in the source code."
    ),
    "NEEDS_REVIEW": (
        "The server returned an unexpected HTTP status code. "
        "Manually open this URL, check the server logs, and verify the page is working correctly."
    ),
}

_DEFAULT_RECOMMENDATION = (
    "Review this issue and verify the affected page or resource is working correctly."
)


def _get_recommendation(issue_type: str) -> str:
    return _RECOMMENDATIONS.get(issue_type, _DEFAULT_RECOMMENDATION)


# ──────────────────────────────────────────────────────────────────────────────
# Issue accumulator (with deduplication)
# ──────────────────────────────────────────────────────────────────────────────

def add_issue(
    issues: list,
    seen_issues: set,
    scan_id,
    issue_type,
    issue_url,
    status_code,
    message,
    severity="LOW",
):
    """
    Append an issue to *issues* and persist it to the database.

    *seen_issues* is a set of (issue_type, issue_url) tuples maintained
    across the entire scan run.  Duplicate entries are silently skipped,
    so re-scanning the same site cannot produce duplicate DB rows.
    """
    dedup_key = (issue_type or "QA_ISSUE", issue_url or "")
    if dedup_key in seen_issues:
        return
    seen_issues.add(dedup_key)

    recommendation = _get_recommendation(issue_type)

    issue = {
        "issue_type":     issue_type or "QA_ISSUE",
        "issue_url":      issue_url,
        "status_code":    status_code,
        "message":        str(message)[:2000],
        "severity":       severity,
        "recommendation": recommendation,
    }
    issues.append(issue)

    try:
        _save_issue_to_db(
            scan_id,
            issue["issue_type"],
            issue["issue_url"],
            issue["status_code"],
            issue["message"],
            issue["severity"],
            issue["recommendation"],
        )
    except Exception as exc:
        print(f"[database] Could not save issue: {exc}")


# ──────────────────────────────────────────────────────────────────────────────
# URL normalisation
# ──────────────────────────────────────────────────────────────────────────────

def normalize_url(url: str) -> str:
    """
    Clean, validate, and SSRF-check a user-supplied URL.
    Raises ValueError with a human-readable message on any failure.
    """
    url = (url or "").strip()

    if not url:
        raise ValueError("Enter a website URL.")

    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    parsed = urlparse(url)

    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError("Enter a valid HTTP or HTTPS URL.")

    if len(url) > 2048:
        raise ValueError("URL is too long (maximum 2 048 characters).")

    hostname = parsed.hostname or ""

    if _is_ssrf_target(hostname):
        raise ValueError(
            f"Scanning internal or private network addresses is not allowed "
            f"({hostname})."
        )

    return url


# ──────────────────────────────────────────────────────────────────────────────
# Single URL checker (used for links and images)
# ──────────────────────────────────────────────────────────────────────────────

def check_url(session: requests.Session, url: str):
    """
    Check a URL with requests and return (result, status_code, reason).
    result is one of: "working" | "broken" | "needs_review"
    """
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
            return "broken", status, f"HTTP {status} – page not found"

        return "needs_review", status, f"HTTP {status}; verify manually"

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


# ──────────────────────────────────────────────────────────────────────────────
# Link analysis
# ──────────────────────────────────────────────────────────────────────────────

def check_links(
    session: requests.Session,
    base_url: str,
    hrefs: list,
    scan_id,
    issues: list,
    seen_issues: set,
):
    total = len(hrefs)
    candidates = []
    seen_urls: set = set()
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

        if full_url not in seen_urls:
            seen_urls.add(full_url)
            candidates.append(full_url)

    limited = candidates[:MAX_LINKS]
    not_checked = max(0, len(candidates) - len(limited))
    skipped += not_checked

    base_host = urlparse(base_url).netloc

    report = {
        "total":             total,
        "unique":            len(candidates),
        "working":           0,
        "broken":            0,
        "needs_review":      0,
        "skipped":           skipped,
        "not_checked_limit": not_checked,
        "checked":           0,
        "internal":          0,
        "external":          0,
        "items":             [],
    }

    for link_url in limited:
        category = (
            "internal"
            if urlparse(link_url).netloc == base_host
            else "external"
        )
        result, status, reason = check_url(session, link_url)

        report[result]    += 1
        report["checked"] += 1
        report[category]  += 1

        report["items"].append({
            "url":         link_url,
            "category":    category,
            "result":      result,
            "status_code": status,
            "reason":      reason,
        })

        if result == "broken":
            add_issue(
                issues, seen_issues, scan_id,
                "BROKEN_LINK", link_url, status, reason, "HIGH",
            )
        elif result == "needs_review":
            add_issue(
                issues, seen_issues, scan_id,
                "NEEDS_REVIEW", link_url, status, reason, "LOW",
            )

    report["skipped_protocol"] = skipped
    return report


# ──────────────────────────────────────────────────────────────────────────────
# Image analysis
# ──────────────────────────────────────────────────────────────────────────────

def check_images(
    session: requests.Session,
    base_url: str,
    sources: list,
    scan_id,
    issues: list,
    seen_issues: set,
):
    candidates = []
    seen_urls: set = set()
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

        if full_url not in seen_urls:
            seen_urls.add(full_url)
            candidates.append(full_url)

    limited = candidates[:MAX_IMAGES]
    not_checked = max(0, len(candidates) - len(limited))
    skipped += not_checked

    report = {
        "total":             len(sources),
        "unique":            len(candidates),
        "working":           0,
        "broken":            0,
        "needs_review":      0,
        "skipped":           skipped,
        "not_checked_limit": not_checked,
        "checked":           0,
        "items":             [],
    }

    for image_url in limited:
        result, status, reason = check_url(session, image_url)

        report[result]    += 1
        report["checked"] += 1

        report["items"].append({
            "url":         image_url,
            "result":      result,
            "status_code": status,
            "reason":      reason,
        })

        if result == "broken":
            add_issue(
                issues, seen_issues, scan_id,
                "BROKEN_IMAGE", image_url, status, reason, "MEDIUM",
            )
        elif result == "needs_review":
            add_issue(
                issues, seen_issues, scan_id,
                "NEEDS_REVIEW", image_url, status, reason, "LOW",
            )

    return report


# ──────────────────────────────────────────────────────────────────────────────
# Performance analysis
# ──────────────────────────────────────────────────────────────────────────────

def analyze_performance(page, navigation_ms: float) -> dict:
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
                    load_time_ms:         load,
                    ttfb_ms:              ttfb,
                    total_resources:      resources.length,
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
            "load_time_ms":         round(navigation_ms),
            "ttfb_ms":              0,
            "total_resources":      0,
            "images":               0,
            "javascript":           0,
            "css":                  0,
            "measurement_complete": False,
            "measurement_note":     str(exc)[:250],
            "navigation_time_ms":   round(navigation_ms),
        }


# ──────────────────────────────────────────────────────────────────────────────
# Health scoring
# ──────────────────────────────────────────────────────────────────────────────

def calculate_health(links: dict, images: dict, javascript: dict) -> dict:
    """
    Health score starts at 100 and deducts points only for CONFIRMED issues.

    Deduction rules:
      • Broken links:      5 pts each, capped at 35
      • Broken images:     3 pts each, capped at 20
      • JS errors (total): 5 pts each, capped at 25

    Unverified links (needs_review) and skipped items do NOT reduce the score.
    """
    score = 100
    deductions = []

    js_errors = (
        javascript.get("console_errors", 0) + javascript.get("page_errors", 0)
    )

    checks = [
        ("broken links",     links.get("broken", 0),   5, 35),
        ("broken images",    images.get("broken", 0),  3, 20),
        ("JavaScript errors", js_errors,                5, 25),
    ]

    for name, count, pts_each, cap in checks:
        pts = min(cap, count * pts_each)
        if pts:
            score -= pts
            deductions.append({
                "reason":          name,
                "count":           count,
                "points_deducted": pts,
            })

    score = max(0, score)

    grade = (
        "A" if score >= 90 else
        "B" if score >= 80 else
        "C" if score >= 70 else
        "D" if score >= 60 else "F"
    )

    status_label = (
        "Excellent"       if score >= 90 else
        "Very Good"       if score >= 75 else
        "Needs Attention" if score >= 50 else
        "Poor"            if score >= 25 else
        "Critical"
    )

    return {
        "score":      score,
        "grade":      grade,
        "status":     status_label,
        "deductions": deductions,
        "note": (
            "Only confirmed broken links, broken images, and JS errors "
            "reduce the score. Unverified or skipped items are not penalised."
        ),
    }


# ──────────────────────────────────────────────────────────────────────────────
# Main scan entry point
# ──────────────────────────────────────────────────────────────────────────────

def run_qa_scan(raw_url: str) -> dict:
    """
    Run a full QA scan on *raw_url* and return the report dict.
    Also writes report.json and persists everything to PostgreSQL.
    """
    url = normalize_url(raw_url)

    issues: list = []
    seen_issues: set = set()   # (issue_type, issue_url) dedup set
    console_errors: list = []
    page_errors: list = []
    failed_requests: list = []
    status_code = None
    page_title = ""
    navigation_timed_out = False
    started = time.perf_counter()

    # ── Browser phase ─────────────────────────────────────────────────────────
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context()
        page    = context.new_page()
        page.set_default_timeout(8000)

        def on_console(msg):
            if msg.type == "error":
                text = msg.text
                # Filter browser noise that is not real JS errors
                if not text.startswith("Failed to load resource:"):
                    console_errors.append(text[:500])

        def on_page_error(error):
            page_errors.append(str(error)[:500])

        def on_request_failed(request):
            failed_requests.append({
                "url":   request.url[:300],
                "error": str(request.failure or "unknown")[:200],
            })

        page.on("console",       on_console)
        page.on("pageerror",     on_page_error)
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

        # Extract all <a href> attributes
        try:
            hrefs = page.locator("a[href]").evaluate_all(
                "(els) => els.map(e => e.getAttribute('href'))"
            )
        except Exception:
            hrefs = []

        # Extract image URLs including lazy-loaded (data-src, srcset, data-srcset)
        try:
            image_sources = page.locator("img").evaluate_all("""
                (els) => els.flatMap(e => {
                    const srcs = [];
                    const main = e.currentSrc || e.getAttribute('src');
                    if (main) srcs.push(main);

                    // Common lazy-load attributes
                    ['data-src', 'data-lazy-src', 'data-original'].forEach(attr => {
                        const v = e.getAttribute(attr);
                        if (v) srcs.push(v);
                    });

                    // srcset / data-srcset: pick the first URL from each candidate
                    const srcset = e.getAttribute('srcset') || e.getAttribute('data-srcset');
                    if (srcset) {
                        srcset.split(',').forEach(part => {
                            const url = part.trim().split(/\\s+/)[0];
                            if (url) srcs.push(url);
                        });
                    }
                    return srcs;
                })
            """)
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

    # ── Save initial scan record (get scan_id for issue FK) ───────────────────
    scan_id = save_scan(url, status_code, page_title)

    # ── HTTP checks (links + images) ──────────────────────────────────────────
    session = requests.Session()
    session.headers.update({"User-Agent": "SmartWebsiteQAAgent/3.1"})

    try:
        links_report  = check_links(
            session, url, hrefs, scan_id, issues, seen_issues
        )
        images_report = check_images(
            session, url, image_sources, scan_id, issues, seen_issues
        )
    finally:
        session.close()

    # ── JavaScript issues ─────────────────────────────────────────────────────
    for error in console_errors:
        add_issue(
            issues, seen_issues, scan_id,
            "CONSOLE_ERROR", url, None, error, "MEDIUM",
        )

    for error in page_errors:
        add_issue(
            issues, seen_issues, scan_id,
            "JAVASCRIPT_RUNTIME_ERROR", url, None, error, "HIGH",
        )

    javascript_report = {
        "console_errors":  len(console_errors),
        "page_errors":     len(page_errors),
        "errors":          console_errors + [f"Runtime: {e}" for e in page_errors],
        "failed_requests": failed_requests[:30],
    }

    # ── Scoring ───────────────────────────────────────────────────────────────
    health = calculate_health(links_report, images_report, javascript_report)

    # ── Assemble full report ──────────────────────────────────────────────────
    report = {
        "scan": {
            "scan_id":              scan_id,
            "url":                  url,
            "status_code":          status_code,
            "page_title":           page_title,
            "scan_time":            datetime.now(timezone.utc).isoformat(),
            "navigation_timed_out": navigation_timed_out,
            "navigation_time_ms":   round(navigation_ms),
        },
        "links":       links_report,
        "images":      images_report,
        "javascript":  javascript_report,
        "performance": performance_report,
        "health":      health,
        "issues":      issues,
    }

    # ── Persist health score + full report to DB ──────────────────────────────
    try:
        update_scan_health(scan_id, health["score"], report)
    except Exception as exc:
        print(f"[database] Could not update scan health: {exc}")

    # ── Write local report.json ───────────────────────────────────────────────
    (BASE_DIR / "report.json").write_text(
        json.dumps(report, indent=2, default=str),
        encoding="utf-8",
    )

    return report


# ──────────────────────────────────────────────────────────────────────────────
# CLI entry point (python api.py https://example.com)
# ──────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Run a QA scan from the command line.")
    parser.add_argument("url", nargs="?", default="https://example.com")
    args = parser.parse_args()

    print(json.dumps(run_qa_scan(args.url), indent=2, default=str))
