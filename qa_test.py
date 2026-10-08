import json
import os
import time

from datetime import datetime
from urllib.parse import urljoin

import psycopg
from dotenv import load_dotenv
from playwright.sync_api import sync_playwright


# ============================================================
# LOAD ENVIRONMENT VARIABLES
# ============================================================

load_dotenv()


# ============================================================
# DATABASE CONFIGURATION
# ============================================================

DB_CONFIG = {
    "host": os.getenv("DB_HOST"),
    "dbname": os.getenv("DB_NAME"),
    "user": os.getenv("DB_USER"),
    "password": os.getenv("DB_PASSWORD"),
}


# ============================================================
# DATABASE CONNECTION
# ============================================================

def get_connection():

    return psycopg.connect(**DB_CONFIG)


# ============================================================
# SAVE SCAN
# ============================================================

def save_scan(
    url,
    status_code,
    page_title
):

    conn = get_connection()

    try:

        cursor = conn.cursor()

        cursor.execute(
            """
            INSERT INTO scans
            (
                website_url,
                status_code,
                page_title
            )
            VALUES (%s, %s, %s)
            RETURNING id
            """,
            (
                url,
                status_code,
                page_title
            )
        )

        scan_id = cursor.fetchone()[0]

        conn.commit()

        return scan_id

    finally:

        cursor.close()
        conn.close()


# ============================================================
# SAVE ISSUE
# ============================================================

def save_issue(
    scan_id,
    issue_type,
    issue_url,
    status_code,
    message,
    severity
):

    conn = get_connection()

    try:

        cursor = conn.cursor()

        cursor.execute(
            """
            INSERT INTO issues
            (
                scan_id,
                issue_type,
                issue_url,
                status_code,
                message,
                severity
            )
            VALUES (%s, %s, %s, %s, %s, %s)
            """,
            (
                scan_id,
                issue_type,
                issue_url,
                status_code,
                message,
                severity
            )
        )

        conn.commit()

    finally:

        cursor.close()
        conn.close()


# ============================================================
# ADD ISSUE
# ============================================================

def add_issue(
    issues,
    scan_id,
    issue_type,
    issue_url,
    status_code,
    message,
    severity
):

    issue = {
        "issue_type": issue_type,
        "issue_url": issue_url,
        "status_code": status_code,
        "message": message,
        "severity": severity
    }

    issues.append(issue)

    if scan_id:

        try:

            save_issue(
                scan_id,
                issue_type,
                issue_url,
                status_code,
                message,
                severity
            )

        except Exception as error:

            print(
                f"Could not save issue: {error}"
            )


# ============================================================
# CHECK LINKS
# ============================================================

def check_links(
    page,
    scan_id,
    issues
):

    print("\n" + "=" * 65)
    print("                         LINK CHECK")
    print("=" * 65)

    links = page.locator("a").all()

    total_links = len(links)

    working_links = 0
    broken_links = 0
    skipped_links = 0

    checked_urls = set()

    print(
        f"Total links found: {total_links}\n"
    )

    for link in links:

        try:

            href = link.get_attribute("href")

            if not href:

                skipped_links += 1

                continue

            href = href.strip()

            if (
                href.startswith("#")
                or href.lower().startswith("mailto:")
                or href.lower().startswith("tel:")
                or href.lower().startswith("javascript:")
                or href.lower().startswith("data:")
            ):

                skipped_links += 1

                continue

            full_url = urljoin(
                page.url,
                href
            )

            if not full_url.startswith(
                ("http://", "https://")
            ):

                skipped_links += 1

                continue

            if full_url in checked_urls:

                continue

            checked_urls.add(full_url)

            try:

                response = page.request.get(
                    full_url,
                    timeout=15000
                )

                status = response.status

                if 200 <= status < 400:

                    working_links += 1

                    if 300 <= status < 400:

                        print(
                            f"REDIRECT {status} - {full_url}"
                        )

                    else:

                        print(
                            f"OK {status} - {full_url}"
                        )

                else:

                    broken_links += 1

                    print(
                        f"BROKEN {status} - {full_url}"
                    )

                    add_issue(
                        issues,
                        scan_id,
                        "BROKEN_LINK",
                        full_url,
                        status,
                        f"Link returned HTTP {status}",
                        "HIGH"
                    )

            except Exception as error:

                broken_links += 1

                error_message = str(error)

                if (
                    "Timeout" in error_message
                    or "timeout" in error_message.lower()
                ):

                    message = "Link request timed out"

                elif (
                    "ERR_NAME_NOT_RESOLVED" in error_message
                    or "ENOTFOUND" in error_message
                    or "ECONNREFUSED" in error_message
                    or "connection" in error_message.lower()
                ):

                    message = "Could not connect to the link"

                else:

                    message = (
                        f"Link request failed: "
                        f"{error_message[:200]}"
                    )

                print(
                    f"BROKEN ERROR - {full_url}"
                )

                print(
                    f"Reason: {message}"
                )

                add_issue(
                    issues,
                    scan_id,
                    "BROKEN_LINK",
                    full_url,
                    None,
                    message,
                    "HIGH"
                )

        except Exception as error:

            broken_links += 1

            print(
                f"Link processing error: {error}"
            )

            add_issue(
                issues,
                scan_id,
                "BROKEN_LINK",
                None,
                None,
                f"Could not process link: {str(error)[:200]}",
                "HIGH"
            )

    print("\n" + "-" * 65)

    print(
        f"Links checked : {len(checked_urls)}"
    )

    print(
        f"Working links : {working_links}"
    )

    print(
        f"Broken links  : {broken_links}"
    )

    print(
        f"Skipped links : {skipped_links}"
    )

    return {
        "total": total_links,
        "working": working_links,
        "broken": broken_links,
        "skipped": skipped_links
    }


# ============================================================
# CHECK IMAGES
# ============================================================

def check_images(
    page,
    scan_id,
    issues
):

    print("\n" + "=" * 65)
    print("                         IMAGE CHECK")
    print("=" * 65)

    images = page.locator("img").all()

    total_images = len(images)

    working_images = 0
    broken_images = 0
    skipped_images = 0

    checked_urls = set()

    print(
        f"Total images found: {total_images}\n"
    )

    for image in images:

        try:

            src = image.get_attribute("src")

            if not src:

                skipped_images += 1

                print(
                    "SKIPPED - Image has no src attribute"
                )

                continue

            src = src.strip()

            if (
                src.lower().startswith("data:")
                or src.lower().startswith("blob:")
            ):

                skipped_images += 1

                print(
                    f"SKIPPED - Non-HTTP image source: "
                    f"{src[:100]}"
                )

                continue

            full_image_url = urljoin(
                page.url,
                src
            )

            if not full_image_url.startswith(
                ("http://", "https://")
            ):

                skipped_images += 1

                print(
                    f"SKIPPED - Unsupported image URL: "
                    f"{full_image_url}"
                )

                continue

            if full_image_url in checked_urls:

                continue

            checked_urls.add(full_image_url)

            try:

                response = page.request.get(
                    full_image_url,
                    timeout=15000
                )

                status = response.status

                if 200 <= status < 400:

                    working_images += 1

                    if 300 <= status < 400:

                        print(
                            f"REDIRECT {status} - "
                            f"{full_image_url}"
                        )

                    else:

                        print(
                            f"OK {status} - "
                            f"{full_image_url}"
                        )

                else:

                    broken_images += 1

                    print(
                        f"BROKEN {status} - "
                        f"{full_image_url}"
                    )

                    add_issue(
                        issues,
                        scan_id,
                        "BROKEN_IMAGE",
                        full_image_url,
                        status,
                        f"Image returned HTTP {status}",
                        "MEDIUM"
                    )

            except Exception as error:

                broken_images += 1

                error_message = str(error)

                if (
                    "Timeout" in error_message
                    or "timeout" in error_message.lower()
                ):

                    message = "Image request timed out"

                elif (
                    "ERR_NAME_NOT_RESOLVED" in error_message
                    or "ENOTFOUND" in error_message
                    or "ECONNREFUSED" in error_message
                    or "connection" in error_message.lower()
                ):

                    message = "Could not connect to the image"

                else:

                    message = (
                        f"Image request failed: "
                        f"{error_message[:200]}"
                    )

                print(
                    f"BROKEN ERROR - "
                    f"{full_image_url}"
                )

                print(
                    f"Reason: {message}"
                )

                add_issue(
                    issues,
                    scan_id,
                    "BROKEN_IMAGE",
                    full_image_url,
                    None,
                    message,
                    "MEDIUM"
                )

        except Exception as error:

            broken_images += 1

            print(
                f"Image processing error: {error}"
            )

            add_issue(
                issues,
                scan_id,
                "BROKEN_IMAGE",
                None,
                None,
                f"Could not process image: {str(error)[:200]}",
                "MEDIUM"
            )

    print("\n" + "-" * 65)

    print(
        f"Images checked : {len(checked_urls)}"
    )

    print(
        f"Working images : {working_images}"
    )

    print(
        f"Broken images  : {broken_images}"
    )

    print(
        f"Skipped images : {skipped_images}"
    )

    return {
        "total": total_images,
        "working": working_images,
        "broken": broken_images,
        "skipped": skipped_images
    }


# ============================================================
# CHECK JAVASCRIPT
# ============================================================

def check_javascript(
    console_errors,
    scan_id,
    issues,
    url
):

    print("\n" + "=" * 65)
    print("                    JAVASCRIPT CHECK")
    print("=" * 65)

    if not console_errors:

        print(
            "No JavaScript console errors detected"
        )

        return {
            "console_errors": 0,
            "errors": []
        }

    print(
        f"{len(console_errors)} "
        "JavaScript console errors detected"
    )

    for error in console_errors:

        print(
            f"ERROR: {error}"
        )

        add_issue(
            issues,
            scan_id,
            "JAVASCRIPT_ERROR",
            url,
            None,
            error,
            "HIGH"
        )

    return {
        "console_errors": len(console_errors),
        "errors": console_errors
    }


# ============================================================
# PAGE PERFORMANCE ANALYSIS
# ============================================================

def analyze_performance(page):

    print("\n" + "=" * 65)
    print("                    PAGE PERFORMANCE")
    print("=" * 65)

    performance = {
        "load_time_ms": 0,
        "total_resources": 0,
        "images": 0,
        "javascript": 0,
        "css": 0
    }

    try:

        performance_data = page.evaluate(
            """
            () => {

                const resources =
                    performance.getEntriesByType("resource");

                let images = 0;
                let javascript = 0;
                let css = 0;

                resources.forEach(resource => {

                    const name =
                        resource.name.toLowerCase();

                    const type =
                        resource.initiatorType;

                    if (
                        type === "img" ||
                        /\\.(png|jpg|jpeg|gif|webp|svg|ico)(\\?|$)/i.test(name)
                    ) {

                        images++;

                    } else if (
                        type === "script" ||
                        /\\.js(\\?|$)/i.test(name)
                    ) {

                        javascript++;

                    } else if (
                        type === "link" ||
                        type === "css" ||
                        /\\.css(\\?|$)/i.test(name)
                    ) {

                        css++;

                    }

                });

                const navigation =
                    performance.getEntriesByType("navigation")[0];

                let loadTime = 0;

                if (navigation) {

                    loadTime =
                        navigation.loadEventEnd -
                        navigation.startTime;

                }

                return {
                    load_time_ms: Math.round(loadTime),
                    total_resources: resources.length,
                    images: images,
                    javascript: javascript,
                    css: css
                };
            }
            """
        )

        performance.update(
            performance_data
        )

    except Exception as error:

        print(
            f"Performance analysis error: {error}"
        )

    print(
        f"Load Time       : "
        f"{performance['load_time_ms']} ms"
    )

    print(
        f"Total Resources : "
        f"{performance['total_resources']}"
    )

    print(
        f"Images          : "
        f"{performance['images']}"
    )

    print(
        f"JavaScript      : "
        f"{performance['javascript']}"
    )

    print(
        f"CSS             : "
        f"{performance['css']}"
    )

    return performance


# ============================================================
# RUN QA SCAN
# ============================================================

def run_qa_scan(url):

    print("=" * 65)
    print("                    SMART WEBSITE QA AGENT")
    print("=" * 65)

    print(
        f"\nTesting: {url}"
    )

    issues = []

    console_errors = []

    status_code = None
    page_title = ""
    scan_id = None

    link_report = {
        "total": 0,
        "working": 0,
        "broken": 0,
        "skipped": 0
    }

    image_report = {
        "total": 0,
        "working": 0,
        "broken": 0,
        "skipped": 0
    }

    javascript_report = {
        "console_errors": 0,
        "errors": []
    }

    performance_report = {
        "load_time_ms": 0,
        "total_resources": 0,
        "images": 0,
        "javascript": 0,
        "css": 0
    }

    with sync_playwright() as playwright:

        browser = playwright.chromium.launch(
            headless=True
        )

        page = browser.new_page()

        def handle_console(message):

            if message.type != "error":

                return

            message_text = message.text

            # Ignore browser resource-loading errors.
            if message_text.startswith(
                "Failed to load resource:"
            ):

                return

            console_errors.append(
                message_text
            )

        def handle_page_error(error):

            console_errors.append(
                f"JavaScript runtime error: {error}"
            )

        page.on(
            "console",
            handle_console
        )

        page.on(
            "pageerror",
            handle_page_error
        )

        try:

            start_time = time.perf_counter()

            response = page.goto(
                url,
                wait_until="networkidle",
                timeout=60000
            )

            navigation_time = (
                time.perf_counter() -
                start_time
            )

            if response:

                status_code = response.status

            page_title = page.title()

            print(
                f"Status Code : {status_code}"
            )

            print(
                f"Page Title  : {page_title}"
            )

            print(
                f"Navigation Time : "
                f"{round(navigation_time * 1000)} ms"
            )

        except Exception as error:

            browser.close()

            raise Exception(
                f"Website loading failed: {error}"
            )

        try:

            scan_id = save_scan(
                url,
                status_code,
                page_title
            )

            print(
                "\nScan saved to PostgreSQL"
            )

            print(
                f"Scan ID: {scan_id}"
            )

        except Exception as error:

            print(
                f"\nDatabase error while saving scan: {error}"
            )

        link_report = check_links(
            page,
            scan_id,
            issues
        )

        image_report = check_images(
            page,
            scan_id,
            issues
        )

        javascript_report = check_javascript(
            console_errors,
            scan_id,
            issues,
            url
        )

        performance_report = analyze_performance(
            page
        )

        try:

            page.screenshot(
                path="homepage.png",
                full_page=True
            )

            print(
                "\nScreenshot saved: homepage.png"
            )

        except Exception as error:

            print(
                f"Screenshot error: {error}"
            )

        browser.close()

    print("\n" + "=" * 65)
    print("                         QA SUMMARY")
    print("=" * 65)

    print("\nLINKS")

    print(
        f"Total Links   : {link_report['total']}"
    )

    print(
        f"Working Links : {link_report['working']}"
    )

    print(
        f"Broken Links  : {link_report['broken']}"
    )

    print(
        f"Skipped Links : {link_report['skipped']}"
    )

    print("\nIMAGES")

    print(
        f"Total Images   : {image_report['total']}"
    )

    print(
        f"Working Images : {image_report['working']}"
    )

    print(
        f"Broken Images  : {image_report['broken']}"
    )

    print(
        f"Skipped Images : {image_report['skipped']}"
    )

    print("\nJAVASCRIPT")

    print(
        f"Console Errors : "
        f"{javascript_report['console_errors']}"
    )

    print("\nPERFORMANCE")

    print(
        f"Load Time       : "
        f"{performance_report['load_time_ms']} ms"
    )

    print(
        f"Total Resources : "
        f"{performance_report['total_resources']}"
    )

    print(
        f"Images          : "
        f"{performance_report['images']}"
    )

    print(
        f"JavaScript      : "
        f"{performance_report['javascript']}"
    )

    print(
        f"CSS             : "
        f"{performance_report['css']}"
    )

    print("\nISSUES")

    print(
        f"Total Issues   : {len(issues)}"
    )

    report = {

        "scan": {
            "scan_id": scan_id,
            "url": url,
            "status_code": status_code,
            "page_title": page_title,
            "scan_time": datetime.now().isoformat()
        },

        "links": link_report,

        "images": image_report,

        "javascript": javascript_report,

        "performance": performance_report,

        "issues": issues
    }

    with open(
        "report.json",
        "w",
        encoding="utf-8"
    ) as file:

        json.dump(
            report,
            file,
            indent=4
        )

    print(
        "\nJSON report saved: report.json"
    )

    print("\n" + "=" * 65)
    print("                    QA SCAN COMPLETED")
    print("=" * 65)

    return report


# ============================================================
# MAIN
# ============================================================

if __name__ == "__main__":

    test_url = "https://httpbin.org"

    run_qa_scan(test_url)