#!/usr/bin/env python3
"""
Bombay High Court Causelist Automation
=======================================
Designed to run on GitHub Actions (or locally).

Credentials are read from environment variables:
  EMAIL_SENDER   — your Gmail address
  EMAIL_PASSWORD — your Gmail App Password

Set these as GitHub Secrets in your repository.
"""

import os
import re
import sys
import smtplib
import tempfile
import datetime
import pdfplumber
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email import encoders
from pathlib import Path

# ─────────────────────────── CONFIGURATION ────────────────────────────────────
RECIPIENT_EMAIL = "didboardcollapse@gmail.com"
EMAIL_SENDER    = os.environ.get("EMAIL_SENDER", "")
EMAIL_PASSWORD  = os.environ.get("EMAIL_PASSWORD", "")
SMTP_HOST       = "smtp.gmail.com"
SMTP_PORT       = 587

SEARCH_KEYWORDS = ["faria", "wadia"]   # case-insensitive
COURT_URL       = "https://bombayhighcourt.gov.in/bhc/causelistFinal"
# ──────────────────────────────────────────────────────────────────────────────


def get_today_formatted():
    return datetime.date.today().strftime("%d-%m-%Y")


def download_causelist_pdf(date_str: str, output_path: str) -> bool:
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

    print(f"[1/4] Opening Bombay High Court causelist portal...")
    print(f"      Date : {date_str}")
    print(f"      URL  : {COURT_URL}")

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"]
        )
        context = browser.new_context(accept_downloads=True)
        page = context.new_page()

        # ── Load the page ────────────────────────────────────────────────────
        try:
            page.goto(COURT_URL, timeout=30000, wait_until="networkidle")
            print("      Page loaded.")
        except PWTimeout:
            print("      WARNING: Page load timed out; attempting to continue...")

        # ── Fill in the date ─────────────────────────────────────────────────
        date_selectors = [
            "input[name*='date' i]",
            "input[id*='date' i]",
            "input[placeholder*='DD' i]",
            "input[placeholder*='date' i]",
            "input[type='text']:first-of-type",
            "input[type='date']",
        ]

        date_filled = False
        for sel in date_selectors:
            try:
                el = page.locator(sel).first
                if el.is_visible(timeout=3000):
                    el.triple_click()
                    el.fill(date_str)
                    print(f"      Date filled via: {sel}")
                    date_filled = True
                    break
            except Exception:
                continue

        if not date_filled:
            debug_html = output_path.replace(".pdf", "_debug.html")
            Path(debug_html).write_text(page.content())
            page.screenshot(path=output_path.replace(".pdf", "_screenshot.png"), full_page=True)
            print(f"      Could not find date input. Debug files saved.")
            browser.close()
            return False

        # ── Click Search ─────────────────────────────────────────────────────
        search_selectors = [
            "button:has-text('Search')",
            "input[type='submit'][value*='Search' i]",
            "button[type='submit']",
            "a:has-text('Search')",
        ]
        for sel in search_selectors:
            try:
                btn = page.locator(sel).first
                if btn.is_visible(timeout=3000):
                    btn.click()
                    page.wait_for_load_state("networkidle", timeout=15000)
                    print(f"      Clicked Search via: {sel}")
                    break
            except Exception:
                continue
        else:
            page.keyboard.press("Enter")
            page.wait_for_load_state("networkidle", timeout=15000)

        # ── Click "Entire Causelist" and capture download ────────────────────
        causelist_selectors = [
            "a:has-text('Entire Causelist')",
            "a:has-text('entire causelist')",
            "a:has-text('Entire')",
            "a[href*='causeList' i]",
            "a[href*='entire' i]",
            "a[href$='.pdf']",
        ]

        downloaded = False
        for sel in causelist_selectors:
            try:
                link = page.locator(sel).first
                if link.is_visible(timeout=5000):
                    with page.expect_download(timeout=60000) as dl_info:
                        link.click()
                    dl_info.value.save_as(output_path)
                    print(f"      PDF saved → {output_path}")
                    downloaded = True
                    break
            except Exception as e:
                print(f"      Selector '{sel}' failed: {e}")

        if not downloaded:
            page.screenshot(path=output_path.replace(".pdf", "_screenshot.png"), full_page=True)
            Path(output_path.replace(".pdf", "_debug.html")).write_text(page.content())
            print("      Could not download PDF. Debug files saved.")
            browser.close()
            return False

        browser.close()

    return os.path.exists(output_path) and os.path.getsize(output_path) > 500


def extract_matching_cases(pdf_path: str, keywords: list) -> list:
    print(f"\n[2/4] Parsing PDF for: {keywords}")
    matches = []
    kw_lower = [k.lower() for k in keywords]

    with pdfplumber.open(pdf_path) as pdf:
        print(f"      Pages: {len(pdf.pages)}")
        for page_num, page in enumerate(pdf.pages, start=1):
            tables = page.extract_tables()
            if tables:
                for table in tables:
                    for row in table:
                        if not row:
                            continue
                        row_text = " ".join(str(c) for c in row if c)
                        if any(kw in row_text.lower() for kw in kw_lower):
                            matches.append({
                                "page": page_num,
                                "type": "table",
                                "data": row,
                                "raw": row_text.strip(),
                            })
            else:
                text = page.extract_text() or ""
                lines = text.split("\n")
                for i, line in enumerate(lines):
                    if any(kw in line.lower() for kw in kw_lower):
                        ctx_start = max(0, i - 3)
                        ctx_end   = min(len(lines), i + 4)
                        context   = "\n".join(lines[ctx_start:ctx_end])
                        matches.append({
                            "page": page_num,
                            "type": "text",
                            "data": None,
                            "raw": context.strip(),
                        })

    print(f"      {len(matches)} match(es) found.")
    return matches


def build_email(matches: list, date_str: str, keywords: list):
    kw_display = " / ".join(k.title() for k in keywords)
    subject = f"BHC Causelist {date_str} — {len(matches)} case(s) for: {kw_display}"

    if not matches:
        plain = (
            f"Bombay High Court Causelist — {date_str}\n"
            f"Search: {kw_display}\n\n"
            "No matching cases found today."
        )
        html = f"""<html><body style="font-family:Arial,sans-serif">
            <h2>Bombay High Court Causelist — {date_str}</h2>
            <p>Search: <b>{kw_display}</b></p>
            <p>No matching cases found today.</p>
        </body></html>"""
        return subject, plain, html

    # Build plain text
    lines = [
        f"Bombay High Court Causelist — {date_str}",
        f"Search terms: {kw_display}",
        f"Matches found: {len(matches)}",
        "=" * 60, "",
    ]

    col_labels = ["Sr.", "Case No.", "Parties", "Advocates", "Bench", "Board", "Subject", "Remarks"]
    html_rows  = ""

    for i, m in enumerate(matches, 1):
        lines.append(f"Case #{i}  (Page {m['page']})")
        lines.append("-" * 40)
        if m["type"] == "table" and m["data"]:
            cells = [str(c).strip() for c in m["data"] if c and str(c).strip()]
            for j, cell in enumerate(cells):
                label = col_labels[j] if j < len(col_labels) else f"Col {j+1}"
                lines.append(f"  {label}: {cell}")
                html_rows += f"<tr><td style='padding:4px 8px;color:#555'><b>{label}</b></td><td style='padding:4px 8px'>{cell}</td></tr>"
        else:
            lines.append(m["raw"])
            html_rows += f"<tr><td colspan='2' style='padding:8px;font-family:monospace;white-space:pre-wrap'>{m['raw']}</td></tr>"
        lines.append("")
        html_rows += "<tr><td colspan='2' style='border-top:1px solid #ddd'></td></tr>"

    plain = "\n".join(lines)
    html = f"""<html><body style="font-family:Arial,sans-serif;font-size:14px;color:#222">
        <div style="background:#8B0000;padding:16px 24px;border-radius:6px 6px 0 0">
            <h2 style="color:#fff;margin:0">Bombay High Court Causelist</h2>
            <p style="color:#ffcdd2;margin:4px 0 0">{date_str}</p>
        </div>
        <div style="padding:16px 24px;background:#fafafa;border:1px solid #ddd;border-top:none;border-radius:0 0 6px 6px">
            <p>Search terms: <b>{kw_display}</b> &nbsp;|&nbsp; <b>{len(matches)}</b> case(s) found</p>
            <table border="0" cellpadding="0" cellspacing="0"
                   style="width:100%;border-collapse:collapse;background:#fff;
                          border:1px solid #ddd;border-radius:4px;font-size:13px">
            {html_rows}
            </table>
            <p style="color:#888;font-size:11px;margin-top:16px">
                Source: <a href="{COURT_URL}">{COURT_URL}</a><br>
                Full causelist PDF is attached.
            </p>
        </div>
    </body></html>"""

    return subject, plain, html


def send_email(subject, plain, html, pdf_path):
    if not EMAIL_SENDER or not EMAIL_PASSWORD:
        print("\n[3/4] Email credentials not set — printing results instead:\n")
        print(plain)
        return

    print(f"\n[3/4] Sending email → {RECIPIENT_EMAIL}")
    msg = MIMEMultipart("mixed")
    msg["Subject"] = subject
    msg["From"]    = EMAIL_SENDER
    msg["To"]      = RECIPIENT_EMAIL

    alt = MIMEMultipart("alternative")
    alt.attach(MIMEText(plain, "plain"))
    alt.attach(MIMEText(html, "html"))
    msg.attach(alt)

    if os.path.exists(pdf_path):
        with open(pdf_path, "rb") as f:
            part = MIMEBase("application", "octet-stream")
            part.set_payload(f.read())
        encoders.encode_base64(part)
        part.add_header("Content-Disposition",
                        f'attachment; filename="{os.path.basename(pdf_path)}"')
        msg.attach(part)

    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as srv:
            srv.ehlo()
            srv.starttls()
            srv.login(EMAIL_SENDER, EMAIL_PASSWORD)
            srv.sendmail(EMAIL_SENDER, RECIPIENT_EMAIL, msg.as_string())
        print("      ✓ Email sent successfully.")
    except Exception as e:
        print(f"      ✗ Email failed: {e}")
        print(plain)


def main():
    print("=" * 60)
    print("  Bombay High Court — Causelist Automation")
    print("=" * 60)

    date_str = get_today_formatted()

    with tempfile.TemporaryDirectory() as tmp:
        pdf_path = os.path.join(tmp, f"bhc_causelist_{date_str.replace('-','')}.pdf")

        ok = download_causelist_pdf(date_str, pdf_path)
        if not ok:
            print("\n[!] PDF download failed. Check debug files or try manually.")
            sys.exit(1)

        matches = extract_matching_cases(pdf_path, SEARCH_KEYWORDS)
        subject, plain, html = build_email(matches, date_str, SEARCH_KEYWORDS)
        send_email(subject, plain, html, pdf_path)

        print(f"\n[4/4] Complete. {len(matches)} case(s) matched.")


if __name__ == "__main__":
    main()
