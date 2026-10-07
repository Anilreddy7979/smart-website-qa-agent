# Smart Website QA Agent

A web-based automated QA testing tool that scans websites and detects common quality issues such as broken links, broken images, and JavaScript console errors.

## Features

- Automated website scanning
- Broken link detection
- Broken image detection
- JavaScript console error detection
- QA health score
- Issue severity detection
- Scan history
- PostgreSQL database integration
- Automatic website screenshot
- JSON scan report
- Modern responsive dashboard

## Tech Stack

### Frontend

- HTML
- CSS
- JavaScript

### Backend

- Python
- FastAPI
- Playwright

### Database

- PostgreSQL

### Tools

- Git
- GitHub
- VS Code

## How It Works

```text
User enters website URL
        ↓
FastAPI receives scan request
        ↓
Playwright opens the website
        ↓
Links and images are checked
        ↓
JavaScript console errors are detected
        ↓
Issues are collected
        ↓
Scan data is stored in PostgreSQL
        ↓
Results are displayed on the dashboard
```
