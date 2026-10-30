"""
qa_test.py – FastAPI application and API endpoints for QA Sentinel.

DB_CONFIG and get_connection() are imported from api.py (single source of
truth).  This file contains only routing, request/response logic, and
application setup.
"""

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel

# DB_CONFIG and get_connection come from api.py — not duplicated here.
from api import (
    ensure_tables,
    get_connection,
    run_qa_scan,
    save_scan,
)

BASE_DIR = Path(__file__).resolve().parent


# ──────────────────────────────────────────────────────────────────────────────
# Lifespan  (replaces the deprecated @app.on_event("startup"))
# ──────────────────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Verify/create DB tables at startup.  No special shutdown needed."""
    try:
        ensure_tables()
        print("[startup] Database schema verified.")
    except Exception as exc:
        print(f"[startup warning] Could not verify database schema: {exc}")
    yield  # application runs here


# ──────────────────────────────────────────────────────────────────────────────
# App
# ──────────────────────────────────────────────────────────────────────────────

app = FastAPI(
    title="Smart Website QA Agent",
    description=(
        "Automated website quality scanning: broken links, broken images, "
        "JavaScript errors, performance, and health scoring."
    ),
    version="3.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],      # Restrict to your domain in production
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ──────────────────────────────────────────────────────────────────────────────
# Request models
# ──────────────────────────────────────────────────────────────────────────────

class ScanRequest(BaseModel):
    url: str


# ──────────────────────────────────────────────────────────────────────────────
# Static assets
# ──────────────────────────────────────────────────────────────────────────────

@app.get("/favicon.svg", include_in_schema=False)
def favicon():
    f = BASE_DIR / "favicon.svg"
    if f.is_file():
        return FileResponse(f, media_type="image/svg+xml")
    raise HTTPException(status_code=404, detail="favicon.svg not found")


# ──────────────────────────────────────────────────────────────────────────────
# Core endpoints
# ──────────────────────────────────────────────────────────────────────────────

@app.get("/", summary="API root")
def home():
    return {
        "status":    "running",
        "version":   "3.1.0",
        "dashboard": "/dashboard",
        "docs":      "/docs",
    }


@app.get("/health", summary="Health check (tests DB connection)")
def health():
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
        return {"api": "ok", "database": "connected"}
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail=f"Database unavailable: {exc}",
        ) from exc


@app.get("/dashboard", summary="Serve the QA Sentinel dashboard")
def dashboard():
    index_file = BASE_DIR / "index.html"
    if not index_file.is_file():
        raise HTTPException(status_code=404, detail="index.html not found")
    return FileResponse(index_file, media_type="text/html")


# ──────────────────────────────────────────────────────────────────────────────
# Scan  (POST)
# ──────────────────────────────────────────────────────────────────────────────

@app.post("/scan", summary="Run a full QA scan on a website")
def scan_website(request: ScanRequest):
    """
    Accepts a URL, runs the full Playwright-based QA scan, persists results
    to PostgreSQL, and returns the complete report.

    Returns HTTP 400 for invalid or blocked URLs.
    Returns HTTP 500 if the scan itself fails.
    """
    url = request.url.strip()

    if not url:
        raise HTTPException(status_code=400, detail="Please enter a website URL.")

    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    try:
        report = run_qa_scan(url)
        return {
            "status":  "success",
            "url":     url,
            "scan_id": report.get("scan", {}).get("scan_id"),
            "message": "QA scan completed successfully.",
            "report":  report,
        }

    except ValueError as exc:
        # normalize_url raises ValueError for invalid or SSRF-blocked URLs
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    except Exception as exc:
        import traceback
        traceback.print_exc()

        # Record the failure in the database so history stays accurate
        try:
            save_scan(
                url, None, "",
                status="failed",
                error_message=str(exc)[:500],
            )
        except Exception as db_error:
            print(f"[scan] Could not save failed scan: {db_error}")

        raise HTTPException(
            status_code=500,
            detail=f"QA scan failed: {exc}",
        ) from exc


# ──────────────────────────────────────────────────────────────────────────────
# Scan history  (GET list)
# ──────────────────────────────────────────────────────────────────────────────

@app.get("/scans", summary="List recent scans (most recent first)")
def get_scans():
    """
    Returns up to 100 most recent scans with issue counts and health scores.
    Used by the dashboard scan-history table.
    """
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT
                        s.id,
                        s.website_url,
                        s.status_code,
                        s.page_title,
                        s.health_score,
                        s.scan_time,
                        s.finished_at,
                        s.status,
                        s.error_message,
                        COUNT(i.id) AS issue_count
                    FROM scans s
                    LEFT JOIN issues i ON i.scan_id = s.id
                    GROUP BY
                        s.id, s.website_url, s.status_code, s.page_title,
                        s.health_score, s.scan_time, s.finished_at,
                        s.status, s.error_message
                    ORDER BY s.id DESC
                    LIMIT 100
                """)
                rows = cur.fetchall()

        scans = [
            {
                "id":            row[0],
                "website_url":   row[1],
                "status_code":   row[2],
                "page_title":    row[3],
                "health_score":  row[4],
                "scan_time":     row[5].isoformat() if row[5] else None,
                "finished_at":   row[6].isoformat() if row[6] else None,
                "status":        row[7],
                "error_message": row[8],
                "issue_count":   row[9],
            }
            for row in rows
        ]

        return {"total": len(scans), "scans": scans}

    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Could not load scans: {exc}",
        ) from exc


# ──────────────────────────────────────────────────────────────────────────────
# Single scan detail  (GET by id)   ← NEW in v3.1
# ──────────────────────────────────────────────────────────────────────────────

@app.get("/scan/{scan_id}", summary="Get full details for a single scan")
def get_scan(scan_id: int):
    """
    Returns the scan metadata, the stored full-report JSON (if available),
    and all issues recorded for that scan.

    Use this endpoint to re-display a previous scan on the dashboard without
    re-running the scan.
    """
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:

                # Scan metadata + stored report
                cur.execute("""
                    SELECT
                        id, website_url, status_code, page_title,
                        health_score, scan_time, finished_at,
                        status, error_message, report
                    FROM scans
                    WHERE id = %s
                """, (scan_id,))
                row = cur.fetchone()

        if not row:
            raise HTTPException(
                status_code=404,
                detail=f"Scan #{scan_id} not found.",
            )

        scan_meta = {
            "id":            row[0],
            "website_url":   row[1],
            "status_code":   row[2],
            "page_title":    row[3],
            "health_score":  row[4],
            "scan_time":     row[5].isoformat() if row[5] else None,
            "finished_at":   row[6].isoformat() if row[6] else None,
            "status":        row[7],
            "error_message": row[8],
        }

        # If the full report JSON is stored, return it directly (fast path)
        stored_report = row[9]
        if stored_report and isinstance(stored_report, dict):
            return {
                "scan":        scan_meta,
                "issue_count": len(stored_report.get("issues", [])),
                "report":      stored_report,
            }

        # Fall back: reconstruct from issues table rows
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT
                        id, issue_type, issue_url, status_code,
                        message, severity, recommendation, created_at
                    FROM issues
                    WHERE scan_id = %s
                    ORDER BY id ASC
                """, (scan_id,))
                issue_rows = cur.fetchall()

        issues = [
            {
                "id":             r[0],
                "scan_id":        scan_id,
                "issue_type":     r[1] or "QA_ISSUE",
                "issue_url":      r[2],
                "status_code":    r[3],
                "message":        r[4] or "",
                "severity":       r[5] or "LOW",
                "recommendation": r[6] or "",
                "created_at":     r[7].isoformat() if r[7] else None,
            }
            for r in issue_rows
        ]

        return {
            "scan":        scan_meta,
            "issue_count": len(issues),
            "issues":      issues,
        }

    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Could not load scan #{scan_id}: {exc}",
        ) from exc


# ──────────────────────────────────────────────────────────────────────────────
# Issues list  (GET)
# ──────────────────────────────────────────────────────────────────────────────

@app.get("/issues", summary="List issues (optionally filter by scan_id)")
def get_issues(scan_id: int | None = None):
    """
    Returns issues.  Pass ?scan_id=N to filter to a specific scan.
    Without a filter, returns the 500 most recent issues across all scans.
    """
    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                if scan_id is None:
                    cur.execute("""
                        SELECT
                            id, scan_id, issue_type, issue_url,
                            status_code, message, severity,
                            recommendation, created_at
                        FROM issues
                        ORDER BY id DESC
                        LIMIT 500
                    """)
                else:
                    cur.execute("""
                        SELECT
                            id, scan_id, issue_type, issue_url,
                            status_code, message, severity,
                            recommendation, created_at
                        FROM issues
                        WHERE scan_id = %s
                        ORDER BY id DESC
                    """, (scan_id,))

                rows = cur.fetchall()

        issues = [
            {
                "id":             row[0],
                "scan_id":        row[1],
                "issue_type":     row[2] or "QA_ISSUE",
                "issue_url":      row[3],
                "status_code":    row[4],
                "message":        row[5] or "",
                "severity":       row[6] or "LOW",
                "recommendation": row[7] or "",
                "created_at":     row[8].isoformat() if row[8] else None,
            }
            for row in rows
        ]

        return {"total": len(issues), "issues": issues}

    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Could not load issues: {exc}",
        ) from exc
