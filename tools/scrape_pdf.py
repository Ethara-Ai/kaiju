"""Scrape library documentation websites into PDF specifications.

For each library, crawls its documentation website using a headless browser,
generates a PDF per page, removes blank pages, merges into a single PDF,
and optionally bz2-compresses the result.

Usage:
    python -m tools.scrape_pdf --url https://docs.python-requests.org/ --name requests
    python -m tools.scrape_pdf --input validated.json --output-dir ./specs
    python -m tools.scrape_pdf --url https://rich.readthedocs.io/ --name rich --compress

Requires:
    pip install playwright PyMuPDF PyPDF2 beautifulsoup4 requests
    playwright install chromium
"""

from __future__ import annotations

import argparse
import bz2
import json
import logging
import os
import re
import shutil
from collections import deque
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urljoin, urlparse

if TYPE_CHECKING:
    import fitz
    import requests as requests_lib
    from bs4 import BeautifulSoup
    from PyPDF2 import PdfMerger
    from playwright.sync_api import Browser, Page

try:
    import fitz  # type: ignore[no-redef]
    import requests as requests_lib  # type: ignore[no-redef]
    from bs4 import BeautifulSoup  # type: ignore[no-redef]
    from PyPDF2 import PdfMerger  # type: ignore[no-redef]
    from playwright.sync_api import sync_playwright  # type: ignore[no-redef]

    _MISSING_DEPS = False
    _MISSING_DEP_MSG = ""
except ImportError as _e:
    _MISSING_DEPS = True
    _MISSING_DEP_MSG = f"scrape_pdf requires: pip install playwright PyMuPDF PyPDF2 beautifulsoup4 requests && playwright install chromium ({_e})"

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

SKIP_URL_PATTERNS: dict[str, list[str]] = {
    "pydantic": ["changelog", "people", "integrations", "migration", "why"],
    "fastapi": ["changelog", "people"],
    "seaborn": [".png"],
}

CAPTCHA_MARKERS = [
    "This website uses a security service to protect against malicious bots",
    "This page is displayed while the website verifies you are not a bot",
    "Checking if the site connection is secure",
    "Enable JavaScript and cookies to continue",
    "Verify you are human",
    "Please verify you are a human",
]

SOFT_404_MARKERS = [
    "the page you requested was not found",
    "this page doesn't exist",
    "nothing to see here",
    "the project you requested does not exist",
    "the page you are looking for could not be found",
    "this page could not be found",
    "we couldn't find the page",
    "the requested page was not found",
]

SOFT_404_FIRST_LINE_EXACT = frozenset(
    [
        "404",
        "page not found",
        "not found",
        "project not found",
        "404 not found",
        "404 error",
        "404 page not found",
    ]
)

_SOFT_404_TITLE_RE = re.compile(
    r"<title[^>]*>\s*"
    r"(404|page\s+not\s+found|project\s+not\s+found|not\s+found)"
    r"\s*(?:[|\-\u2014:]|</title>)",
    re.IGNORECASE,
)
_SOFT_404_H1_RE = re.compile(
    r"<h1[^>]*>\s*"
    r"(404|page\s+not\s+found|project\s+not\s+found|not\s+found)"
    r"\s*</h1>",
    re.IGNORECASE,
)

FASTAPI_NON_ENGLISH_PREFIXES = frozenset(
    [
        "az",
        "bn",
        "de",
        "es",
        "fa",
        "fr",
        "he",
        "hu",
        "id",
        "it",
        "ja",
        "ko",
        "pl",
        "pt",
        "ru",
        "tr",
        "uk",
        "ur",
        "vi",
        "yo",
        "zh",
        "zh-hant",
        "em",
    ]
)


def _is_page_blank(page: Any) -> bool:
    text = page.get_text("text")
    return not text.strip()


def _is_captcha_page(page: Any) -> bool:
    """Check if a PDF page contains bot-verification / CAPTCHA content."""
    text = page.get_text("text")
    text_lower = text.lower()
    return any(marker.lower() in text_lower for marker in CAPTCHA_MARKERS)


def _is_soft_404_page(page: Any) -> bool:
    """Check if a PDF page contains soft-404 content (short page with 404 markers)."""
    text = page.get_text("text")
    text_lower = text.lower().strip()
    if len(text_lower) > 500:
        return False
    if any(marker in text_lower for marker in SOFT_404_MARKERS):
        return True
    first_line = text_lower.split("\n")[0].strip().rstrip(".!:;, ")
    return first_line in SOFT_404_FIRST_LINE_EXACT


def _is_soft_404_content(html: str) -> bool:
    """Check if raw HTML content indicates a soft-404 (HTTP 200 with not-found body)."""
    if _SOFT_404_TITLE_RE.search(html):
        return True
    if _SOFT_404_H1_RE.search(html):
        return True
    return False


_CLOUDFLARE_MARKERS = (
    "cdn-cgi/challenge-platform",
    "cf-browser-verification",
    "cf_chl_opt",
    "Checking your browser",
    "Attention Required! | Cloudflare",
    "Just a moment...",
    "_cf_chl_",
)


def _is_cloudflare_challenge(html: str) -> bool:
    return any(marker in html for marker in _CLOUDFLARE_MARKERS)


def _remove_blank_pages(pdf_path: str) -> None:
    document = fitz.open(pdf_path)
    if document.page_count < 2:
        document.close()
        return

    output_document = fitz.open()
    removed_captcha = 0
    removed_soft_404 = 0
    try:
        for i in range(document.page_count):
            page = document.load_page(i)
            if _is_page_blank(page):
                continue
            if _is_captcha_page(page):
                removed_captcha += 1
                continue
            if _is_soft_404_page(page):
                removed_soft_404 += 1
                continue
            output_document.insert_pdf(document, from_page=i, to_page=i)

        if removed_captcha:
            logger.info(
                "  Removed %d captcha/bot-check page(s) from %s", removed_captcha, pdf_path
            )
        if removed_soft_404:
            logger.info("  Removed %d soft-404 page(s) from %s", removed_soft_404, pdf_path)

        document.close()
        output_document.save(pdf_path)
    finally:
        output_document.close()
        if not document.is_closed:
            document.close()


def _clean_pdf_directory(docs: list[str]) -> None:
    for doc in docs:
        if os.path.exists(doc):
            _remove_blank_pages(doc)


def _is_valid_link(link: str, base_url: str) -> str | None:
    parsed_url = urlparse(link)
    if parsed_url.fragment:
        return None
    if not parsed_url.scheme:
        return urljoin(base_url, link)
    if parsed_url.netloc == urlparse(base_url).netloc:
        return link
    return None


# Generic URL path segments that indicate auth/login pages (never useful for docs).
_AUTH_PATH_SEGMENTS = frozenset(
    [
        "login",
        "logout",
        "signin",
        "signout",
        "sign-in",
        "sign-out",
        "signup",
        "sign-up",
        "register",
        "auth",
        "oauth",
        "sso",
        "callback",
        "reset-password",
        "forgot-password",
        "verify-email",
    ]
)


def _should_skip_url(current_url: str, base_url: str) -> bool:
    # Per-site pattern filtering
    for site_key, patterns in SKIP_URL_PATTERNS.items():
        if site_key in base_url:
            if any(p in current_url for p in patterns):
                return True

    if "fastapi" in base_url:
        stripped = current_url.replace("https://", "")
        parts = [x for x in stripped.split("/") if x]
        if len(parts) > 1 and parts[1] in FASTAPI_NON_ENGLISH_PREFIXES:
            return True

    # Generic: skip auth/login pages on ANY site
    parsed_path = urlparse(current_url).path.lower().strip("/")
    path_segments = parsed_path.split("/")
    if any(seg in _AUTH_PATH_SEGMENTS for seg in path_segments):
        logger.debug("  Skipping auth/login URL: %s", current_url)
        return True

    # Skip URLs with login-related query parameters (e.g., ?redirect_uri=...)
    query = urlparse(current_url).query.lower()
    if "redirect_uri=" in query or "return_to=" in query or "next=" in query:
        logger.debug("  Skipping redirect URL: %s", current_url)
        return True

    return False


def _generate_pdf(page: Any, url: str, output_dir: str) -> str:
    pdf_path = ""
    try:
        try:
            response = page.goto(url, wait_until="networkidle", timeout=30000)
        except Exception:
            logger.debug(
                "  networkidle timeout for %s, retrying with domcontentloaded", url
            )
            response = page.goto(url, wait_until="domcontentloaded", timeout=15000)

        if response and response.status >= 400:
            logger.debug(
                "  HTTP %d generating PDF for %s, skipping", response.status, url
            )
            return pdf_path

        out_name = f"{urlparse(url).path.replace('/', '_').strip('_')}.pdf"
        if out_name == ".pdf":
            out_name = "base.pdf"
        pdf_path = os.path.join(output_dir, out_name)

        page.pdf(
            path=pdf_path,
            print_background=True,
            format="A4",
            margin={"top": "0px", "bottom": "0px", "left": "0px", "right": "0px"},
        )
        logger.debug("  Saved PDF: %s", pdf_path)
    except Exception as e:
        logger.warning("  Error creating PDF for %s: %s", url, e)
    return pdf_path


def _crawl_website(
    browser: Any, base_url: str, output_dir: str, max_pages: int = 500
) -> list[str]:
    page = browser.new_page()
    visited: set[str] = set()
    to_visit = deque([base_url])
    sequence: list[str] = []
    pages_scraped = 0

    while to_visit and pages_scraped < max_pages:
        current_url = to_visit.popleft()

        if _should_skip_url(current_url, base_url):
            continue

        if current_url in visited:
            continue

        logger.info("  Crawling: %s", current_url)
        visited.add(current_url)

        try:
            response = page.goto(
                current_url, wait_until="domcontentloaded", timeout=30000
            )
            if response and response.status >= 400:
                logger.debug("  HTTP %d: %s", response.status, current_url)
                continue

            content = page.content()

            if _is_cloudflare_challenge(content):
                logger.warning("  Cloudflare challenge detected, aborting crawl: %s", current_url)
                break

            if _is_soft_404_content(content):
                logger.info("  Soft-404 detected, skipping: %s", current_url)
                continue

            soup = BeautifulSoup(content, "html.parser")

            for link in soup.find_all("a", href=True):
                full_url = _is_valid_link(link["href"], base_url)
                if (
                    full_url
                    and full_url not in visited
                    and full_url.startswith(base_url)
                ):
                    to_visit.append(full_url)

            pdf = _generate_pdf(page, current_url, output_dir)
            if pdf:
                sequence.append(pdf)
            pages_scraped += 1
        except Exception as e:
            logger.warning("  Error crawling %s: %s", current_url, e)

    page.close()
    return sequence


def _merge_pdfs(docs: list[str], output_filename: str) -> None:
    merger = PdfMerger()
    for pdf in docs:
        if os.path.exists(pdf):
            merger.append(pdf)
    merger.write(output_filename)
    merger.close()


def _compress_bz2(input_path: str, output_path: str) -> None:
    with open(input_path, "rb") as f_in:
        with bz2.open(output_path, "wb") as f_out:
            f_out.writelines(f_in)


def scrape_spec(
    base_url: str,
    name: str,
    output_dir: str = "specs",
    compress: bool = True,
) -> str | None:
    if _MISSING_DEPS:
        raise ImportError(_MISSING_DEP_MSG)

    blocked = {"github.com", "github.io", "gitlab.com", "bitbucket.org", "pypi.org", "wikipedia.org"}
    domain = urlparse(base_url).netloc.lower()
    if any(domain == b or domain.endswith("." + b) for b in blocked):
        logger.warning("  Blocked domain %s — skipping spec scrape for %s", domain, name)
        return None

    os.makedirs(output_dir, exist_ok=True)
    pages_dir = os.path.join(output_dir, f"{name}_pages")
    final_pdf = os.path.join(output_dir, f"{name}.pdf")

    url_parts = [x for x in base_url.split("/") if x]
    if url_parts and url_parts[-1] == "pdf":
        logger.info("  Direct PDF download: %s", base_url)
        try:
            response = requests_lib.get(base_url, timeout=60)
            response.raise_for_status()
            with open(final_pdf, "wb") as f:
                f.write(response.content)
        except Exception as e:
            logger.error("  Failed to download PDF: %s", e)
            return None
    else:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                os.makedirs(pages_dir, exist_ok=True)
                pdfs = _crawl_website(browser, base_url, pages_dir)
                if not pdfs:
                    logger.warning("  No pages crawled for %s", name)
                    return None

                _clean_pdf_directory(pdfs)
                _merge_pdfs(pdfs, final_pdf)
            finally:
                browser.close()
                if os.path.isdir(pages_dir):
                    shutil.rmtree(pages_dir, ignore_errors=True)

    if not os.path.exists(final_pdf):
        return None

    if compress:
        compressed_path = f"{final_pdf}.bz2"
        _compress_bz2(final_pdf, compressed_path)
        os.remove(final_pdf)
        logger.info("  Spec saved: %s", compressed_path)
        return compressed_path

    logger.info("  Spec saved: %s", final_pdf)
    return final_pdf


# Alias for backward compatibility (was async, now sync)
scrape_spec_sync = scrape_spec


def main() -> None:
    parser = argparse.ArgumentParser(description="Scrape library docs into PDF specs")
    parser.add_argument("--url", type=str, help="Documentation URL to scrape")
    parser.add_argument(
        "--name", type=str, help="Library name (used for output filename)"
    )
    parser.add_argument(
        "--input",
        type=str,
        help="Input JSON (validated.json or dataset_entries.json) with specification URLs",
    )
    parser.add_argument(
        "--output-dir",
        type=str,
        default="./specs",
        help="Output directory for PDFs (default: ./specs)",
    )
    parser.add_argument(
        "--no-compress",
        action="store_true",
        help="Skip bz2 compression of output PDFs",
    )
    parser.add_argument(
        "--max-repos",
        type=int,
        default=None,
        help="Max repos to scrape specs for",
    )

    args = parser.parse_args()

    if args.url and args.name:
        result = scrape_spec(args.url, args.name, args.output_dir, not args.no_compress)
        if result:
            print(f"Done: {result}")
        else:
            print("Failed to scrape spec")
            exit(1)

    elif args.input:
        entries = json.loads(Path(args.input).read_text(encoding="utf-8"))

        if isinstance(entries, dict) and "data" in entries:
            entries = entries["data"]

        count = 0
        for entry in entries:
            if args.max_repos and count >= args.max_repos:
                break

            spec_url = None
            name = None

            if isinstance(entry, dict):
                name = (
                    entry.get("instance_id", "").split("/")[-1]
                    or entry.get("name", "").split("/")[-1]
                )

                if "setup" in entry and isinstance(entry["setup"], dict):
                    spec_url = entry["setup"].get("specification")
                elif "analysis" in entry and isinstance(entry.get("analysis"), dict):
                    spec_url = entry["analysis"].get("docs_url")
                elif "specification" in entry:
                    spec_url = entry["specification"]

            if not spec_url or not name:
                logger.warning(
                    "  Skipping entry — no spec URL or name: %s",
                    entry.get("instance_id", "?"),
                )
                continue

            logger.info("\nScraping spec for %s: %s", name, spec_url)
            result = scrape_spec(spec_url, name, args.output_dir, not args.no_compress)
            if result:
                count += 1
                logger.info("  [%d] Done: %s", count, result)
            else:
                logger.warning("  Failed: %s", name)

        print(f"\nScraped {count} specs to {args.output_dir}")

    else:
        parser.error("Provide either --url/--name or --input")


if __name__ == "__main__":
    main()
