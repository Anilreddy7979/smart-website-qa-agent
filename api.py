from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel
import os

import psycopg
from dotenv import load_dotenv


# Load environment variables
load_dotenv()


app = FastAPI(
    title="Smart Website QA Agent",
    description="API for automated website quality testing",
    version="1.0.0"
)


# Allow the dashboard to communicate with the API
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# Database configuration
DB_CONFIG = {
    "host": os.getenv("DB_HOST"),
    "dbname": os.getenv("DB_NAME"),
    "user": os.getenv("DB_USER"),
    "password": os.getenv("DB_PASSWORD"),
}


def get_connection():
    return psycopg.connect(**DB_CONFIG)


class ScanRequest(BaseModel):
    url: str


def save_scan_to_database(scan):
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
                scan.get("url"),
                scan.get("status_code"),
                scan.get("page_title")
            )
        )

        scan_id = cursor.fetchone()[0]

        conn.commit()

        return scan_id

    finally:
        cursor.close()
        conn.close()


def save_issues_to_database(scan_id, issues):
    if not issues:
        return

    conn = get_connection()

    try:
        cursor = conn.cursor()

        for issue in issues:

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
                    issue.get("issue_type", "UNKNOWN"),
                    issue.get("issue_url"),
                    issue.get("status_code"),
                    issue.get("message"),
                    issue.get("severity", "MEDIUM")
                )
            )

        conn.commit()

    finally:
        cursor.close()
        conn.close()


@app.get("/")
def home():
    return {
        "status": "running",
        "message": "Smart Website QA Agent API is running"
    }


@app.get("/dashboard")
def dashboard():
    return FileResponse("index.html")


@app.post("/scan")
def scan_website(request: ScanRequest):

    url = request.url.strip()

    if not url.startswith(("http://", "https://")):
        raise HTTPException(
            status_code=400,
            detail="URL must start with http:// or https://"
        )

    try:

        from qa_test import run_qa_scan

        # Run the website QA scan
        result = run_qa_scan(url)

        # Save scan information
        scan_id = save_scan_to_database(
            result["scan"]
        )

        # Save detected issues
        save_issues_to_database(
            scan_id,
            result.get("issues", [])
        )

        # Add database ID to the report
        result["scan"]["scan_id"] = scan_id

        return {
            "status": "success",
            "url": url,
            "message": "QA scan completed successfully",
            "report": result
        }

    except Exception as error:

        raise HTTPException(
            status_code=500,
            detail=str(error)
        )


@app.get("/scans")
def get_scans():

    try:

        conn = get_connection()

        try:

            cursor = conn.cursor()

            cursor.execute(
                """
                SELECT
                    s.id,
                    s.website_url,
                    s.status_code,
                    s.page_title,
                    s.scan_time,
                    COUNT(i.id) AS issue_count
                FROM scans s
                LEFT JOIN issues i
                    ON s.id = i.scan_id
                GROUP BY
                    s.id,
                    s.website_url,
                    s.status_code,
                    s.page_title,
                    s.scan_time
                ORDER BY s.id DESC
                """
            )

            rows = cursor.fetchall()

        finally:

            cursor.close()
            conn.close()


        scans = []

        for row in rows:

            scans.append(
                {
                    "id": row[0],
                    "website_url": row[1],
                    "status_code": row[2],
                    "page_title": row[3],
                    "scan_time": row[4],
                    "issue_count": row[5]
                }
            )


        return {
            "total": len(scans),
            "scans": scans
        }


    except Exception as error:

        raise HTTPException(
            status_code=500,
            detail=str(error)
        )


@app.get("/issues")
def get_issues():

    try:

        conn = get_connection()

        try:

            cursor = conn.cursor()

            cursor.execute(
                """
                SELECT
                    id,
                    scan_id,
                    issue_type,
                    issue_url,
                    status_code,
                    message,
                    severity,
                    created_at
                FROM issues
                ORDER BY id DESC
                """
            )

            rows = cursor.fetchall()

        finally:

            cursor.close()
            conn.close()


        issues = []

        for row in rows:

            issues.append(
                {
                    "id": row[0],
                    "scan_id": row[1],
                    "issue_type": row[2],
                    "issue_url": row[3],
                    "status_code": row[4],
                    "message": row[5],
                    "severity": row[6],
                    "created_at": row[7]
                }
            )


        return {
            "total": len(issues),
            "issues": issues
        }


    except Exception as error:

        raise HTTPException(
            status_code=500,
            detail=str(error)
        )