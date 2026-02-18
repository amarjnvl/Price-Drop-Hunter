"""
Price Drop Hunter 🎯 — Telegram + Google Sheets Edition

Dual-mode operation:
  • Webhook mode (--serve):  Flask app on Render.com for instant Telegram responses
  • Standalone mode:         Classic 3-phase script for GitHub Actions / local runs

Environment variables:
  TELEGRAM_TOKEN     – Bot token from @BotFather
  CHAT_ID            – Your Telegram chat / group ID
  GOOGLE_CREDENTIALS – Full JSON content of service account credentials
  SHEET_ID           – Google Sheet ID (from the URL)
  WEBHOOK_SECRET     – Secret token to verify webhook requests (optional)
"""

import json
import os
import re
import sys
import logging
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests
from flask import Flask, request as flask_request, jsonify
from bs4 import BeautifulSoup
from dotenv import load_dotenv
import gspread
from google.oauth2.service_account import Credentials
import feedparser
import google.generativeai as genai

# Load .env file (for local testing; ignored in GitHub Actions)
load_dotenv()

# ──────────────────────────── Logging ────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("price-drop-hunter")

# ──────────────────────────── Config ─────────────────────────────
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
CHAT_ID = os.environ.get("CHAT_ID", "")
GOOGLE_CREDENTIALS = os.environ.get("GOOGLE_CREDENTIALS", "")
SHEET_ID = os.environ.get("SHEET_ID", "")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")

# Indian Standard Time (UTC+5:30)
IST = timezone(timedelta(hours=5, minutes=30))


def now_ist() -> datetime:
    """Return the current time in IST."""
    return datetime.now(IST)


HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/125.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-IN,en;q=0.9",
    "Accept-Encoding": "gzip, deflate",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Connection": "keep-alive",
}

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]


# ──────────────────────────── Validation ─────────────────────────
def validate_config() -> None:
    """Ensure all required env vars are set."""
    required = {
        "TELEGRAM_TOKEN": TELEGRAM_TOKEN,
        "CHAT_ID": CHAT_ID,
        "GOOGLE_CREDENTIALS": GOOGLE_CREDENTIALS,
        "SHEET_ID": SHEET_ID,
    }
    missing = [name for name, val in required.items() if not val]
    if missing:
        log.error("Missing environment variable(s): %s", ", ".join(missing))
        sys.exit(1)


# ──────────────────────────── Google Sheets ──────────────────────
def connect_to_sheet() -> gspread.Spreadsheet:
    """Authenticate with Google and return the spreadsheet."""
    creds_dict = json.loads(GOOGLE_CREDENTIALS)
    creds = Credentials.from_service_account_info(creds_dict, scopes=SCOPES)
    client = gspread.authorize(creds)
    sheet = client.open_by_key(SHEET_ID)
    log.info("📊 Connected to Google Sheet: %s", sheet.title)
    return sheet


def get_products_worksheet(sheet: gspread.Spreadsheet) -> gspread.Worksheet:
    """Get or create the 'Products' tab with 6 columns."""
    try:
        ws = sheet.worksheet("Products")
    except gspread.WorksheetNotFound:
        ws = sheet.add_worksheet(title="Products", rows=100, cols=6)
        ws.update("A1:F1", [["Name", "URL", "Target_Price", "Current_Price",
                              "Last_Alerted", "Status"]])
        log.info("Created 'Products' tab with headers.")
        return ws

    # Auto-migrate: add missing columns E, F if sheet was created before
    headers = ws.row_values(1)
    if len(headers) < 5:
        ws.update_acell("E1", "Last_Alerted")
    if len(headers) < 6:
        ws.update_acell("F1", "Status")
    return ws


def get_settings_worksheet(sheet: gspread.Spreadsheet) -> gspread.Worksheet:
    """Get or create the 'Settings' tab."""
    try:
        ws = sheet.worksheet("Settings")
    except gspread.WorksheetNotFound:
        ws = sheet.add_worksheet(title="Settings", rows=10, cols=1)
        ws.update_acell("A1", "0")
        log.info("Created 'Settings' tab with last_update_id = 0.")
    return ws


def get_history_worksheet(sheet: gspread.Spreadsheet) -> gspread.Worksheet:
    """Get or create the 'Price_History' tab."""
    try:
        ws = sheet.worksheet("Price_History")
    except gspread.WorksheetNotFound:
        ws = sheet.add_worksheet(title="Price_History", rows=1000, cols=4)
        ws.update("A1:D1", [["Date", "Product", "Price", "Target"]])
        log.info("Created 'Price_History' tab with headers.")
    return ws


def log_price_history(
    history_ws: gspread.Worksheet,
    name: str,
    price: float,
    target: float,
) -> None:
    """Append a row to the Price_History tab."""
    now = now_ist().strftime("%Y-%m-%d %H:%M")
    try:
        history_ws.append_row([now, name, f"{price:.2f}", f"{target:.0f}"])
    except Exception as exc:
        log.warning("Could not log to Price_History: %s", exc)


def get_last_update_id(settings_ws: gspread.Worksheet) -> int:
    """Read the last processed Telegram update ID from Settings!A1."""
    val = settings_ws.acell("A1").value
    return int(val) if val and val.isdigit() else 0


def set_last_update_id(settings_ws: gspread.Worksheet, update_id: int) -> None:
    """Write the last processed update ID to Settings!A1."""
    settings_ws.update_acell("A1", str(update_id))


# ──────────────────────────── Price Helpers ──────────────────────
def extract_price(text: str) -> float | None:
    """Pull the first price-like number from a string."""
    cleaned = text.replace(",", "").strip()
    match = re.search(r"[\d]+\.?\d*", cleaned)
    return float(match.group()) if match else None


def detect_platform(url: str) -> str:
    """Return 'amazon' or 'flipkart' based on URL, or 'unknown'."""
    lower = url.lower()
    if "amazon" in lower:
        return "amazon"
    if "flipkart" in lower:
        return "flipkart"
    return "unknown"


# ──────────────────────────── Scraping ───────────────────────────
def fetch_page(url: str) -> BeautifulSoup | None:
    """Fetch a URL and return a BeautifulSoup object."""
    try:
        resp = requests.get(url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
        return BeautifulSoup(resp.text, "html.parser")
    except requests.RequestException as exc:
        log.error("HTTP request failed for %s: %s", url, exc)
        return None


# ── Title Extraction (multi-strategy) ──

def extract_title_from_json_ld(soup: BeautifulSoup) -> str | None:
    """Extract title from JSON-LD structured data (works on both sites)."""
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
            # Handle both single objects and arrays
            items = data if isinstance(data, list) else [data]
            for item in items:
                if not isinstance(item, dict):
                    continue
                # Match by @type OR by having both 'name' and 'offers' keys
                is_product = (
                    item.get("@type") == "Product"
                    or (item.get("name") and item.get("offers"))
                )
                if is_product and item.get("name"):
                    return item["name"].strip()
        except (json.JSONDecodeError, AttributeError, TypeError):
            continue
    return None


def extract_title_from_meta(soup: BeautifulSoup) -> str | None:
    """Extract title from og:title meta tag."""
    tag = soup.find("meta", property="og:title")
    if tag and tag.get("content"):
        return tag["content"].strip()
    return None


def extract_title_from_page(soup: BeautifulSoup) -> str | None:
    """Extract and clean the <title> tag."""
    if soup.title and soup.title.string:
        raw = soup.title.string.strip()
        # Clean common suffixes
        for sep in [" - Buy ", " : Amazon", " | Amazon", " - Amazon",
                    " Price in India", " at Best Price", " Online at"]:
            if sep in raw:
                raw = raw[:raw.index(sep)]
        return raw.strip() if raw else None
    return None


def scrape_title(soup: BeautifulSoup, platform: str) -> str | None:
    """Try multiple strategies to get the product title."""
    # Strategy 1: JSON-LD structured data (most reliable)
    title = extract_title_from_json_ld(soup)
    if title:
        log.info("   📛 Title from JSON-LD: %s", title[:60])
        return title

    # Strategy 2: Platform-specific CSS selectors
    if platform == "amazon":
        tag = soup.select_one("span#productTitle")
        if tag:
            title = tag.get_text().strip()
            if title:
                log.info("   📛 Title from #productTitle")
                return title

    if platform == "flipkart":
        for css in ["span.VU-ZEz", "h1.yhB1nd", "span.B_NuCI", "h1._9E25nV"]:
            tag = soup.select_one(css)
            if tag:
                title = tag.get_text().strip()
                if title:
                    log.info("   📛 Title from CSS: %s", css)
                    return title

    # Strategy 3: og:title meta tag
    title = extract_title_from_meta(soup)
    if title:
        log.info("   📛 Title from og:title")
        return title

    # Strategy 4: <title> tag (fallback, always present)
    title = extract_title_from_page(soup)
    if title:
        log.info("   📛 Title from <title> tag")
        return title

    return None


# ── Price Extraction (multi-strategy) ──

def extract_price_from_json_ld(soup: BeautifulSoup) -> float | None:
    """Extract price from JSON-LD structured data."""
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
            items = data if isinstance(data, list) else [data]
            for item in items:
                if not isinstance(item, dict):
                    continue
                # Match by @type OR by having 'offers' key
                is_product = (
                    item.get("@type") == "Product"
                    or item.get("offers")
                )
                if not is_product:
                    continue
                offers = item.get("offers", {})
                # Could be a single offer or a list
                if isinstance(offers, list):
                    offers = offers[0] if offers else {}
                price = offers.get("price") or offers.get("lowPrice")
                if price:
                    return float(str(price).replace(",", ""))
        except (json.JSONDecodeError, AttributeError, ValueError, TypeError):
            continue
    return None


def extract_price_from_meta(soup: BeautifulSoup) -> float | None:
    """Extract price from meta tags (product:price:amount or og:price:amount)."""
    for prop in ["product:price:amount", "og:price:amount"]:
        tag = soup.find("meta", property=prop)
        if tag and tag.get("content"):
            try:
                return float(tag["content"].replace(",", ""))
            except ValueError:
                continue
    return None


def extract_price_from_html_regex(soup: BeautifulSoup, html_text: str) -> float | None:
    """
    Last resort: find ₹X,XXX patterns in the HTML.
    Returns the LOWEST price found (likely the sale/deal price).
    """
    # Match ₹ followed by a price — e.g. ₹1,199 or ₹55,999.00
    matches = re.findall(r"₹\s*([\d,]+(?:\.\d{1,2})?)", html_text)
    if not matches:
        return None

    prices = []
    for m in matches:
        try:
            prices.append(float(m.replace(",", "")))
        except ValueError:
            continue

    # Filter out tiny values (like ₹19 protect fees) and huge values
    valid = [p for p in prices if p >= 50]
    if valid:
        # The lowest price is typically the sale price
        return min(valid)
    return None


def scrape_price(soup: BeautifulSoup, platform: str, html_text: str) -> float | None:
    """Try multiple strategies to get the product price."""
    # Strategy 1: JSON-LD structured data (most reliable)
    price = extract_price_from_json_ld(soup)
    if price:
        log.info("   💲 Price from JSON-LD")
        return price

    # Strategy 2: Platform-specific CSS selectors
    if platform == "amazon":
        for css in ["span.a-price-whole", "span#priceblock_dealprice",
                     "span#priceblock_ourprice", "span.a-offscreen",
                     "div#corePrice_feature_div span.a-price-whole",
                     "span.priceToPay span.a-price-whole"]:
            tag = soup.select_one(css)
            if tag:
                p = extract_price(tag.get_text())
                if p:
                    log.info("   💲 Price from CSS: %s", css)
                    return p

    if platform == "flipkart":
        for css in ["div.Nx9bqj.CxhGGd", "div._30jeq3._16Jk6d",
                     "div._30jeq3", "div.Nx9bqj"]:
            tag = soup.select_one(css)
            if tag:
                p = extract_price(tag.get_text())
                if p:
                    log.info("   💲 Price from CSS: %s", css)
                    return p

    # Strategy 3: Meta tags
    price = extract_price_from_meta(soup)
    if price:
        log.info("   💲 Price from meta tag")
        return price

    # Strategy 4: Regex on full HTML (last resort)
    price = extract_price_from_html_regex(soup, html_text)
    if price:
        log.info("   💲 Price from HTML regex (₹ pattern)")
        return price

    return None


# ── Main scraper ──

def scrape_product_info(url: str) -> dict | None:
    """
    Scrape a product page and return {'title': ..., 'price': ...}.
    Uses multiple strategies: JSON-LD → CSS selectors → meta tags → regex.
    Returns None if the page can't be fetched.
    """
    platform = detect_platform(url)
    if platform == "unknown":
        log.warning("Unsupported platform: %s", url)
        return None

    try:
        resp = requests.get(url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
    except requests.RequestException as exc:
        log.error("HTTP request failed for %s: %s", url, exc)
        return None

    html_text = resp.text
    log.info("   📄 HTTP %d | %d chars | JSON-LD: %s | ₹: %s",
             resp.status_code, len(html_text),
             "YES" if "application/ld+json" in html_text else "NO",
             "YES" if "₹" in html_text else "NO")

    soup = BeautifulSoup(html_text, "html.parser")

    title = scrape_title(soup, platform)
    price = scrape_price(soup, platform, html_text)

    return {"title": title, "price": price}


# ──────────────────────────── Telegram ───────────────────────────
def get_telegram_updates(last_update_id: int) -> list[dict]:
    """Fetch new messages from Telegram using getUpdates."""
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
    params = {"offset": last_update_id + 1, "timeout": 5}

    try:
        resp = requests.get(url, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        if data.get("ok"):
            return data.get("result", [])
    except requests.RequestException as exc:
        log.error("Failed to fetch Telegram updates: %s", exc)
    return []


def send_telegram_message(message: str, chat_id: str = "") -> bool:
    """Send a message via the Telegram Bot API."""
    api_url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": chat_id or CHAT_ID,
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }

    try:
        resp = requests.post(api_url, json=payload, timeout=15)
        resp.raise_for_status()
        log.info("✅ Telegram message sent successfully.")
        return True
    except requests.RequestException as exc:
        log.error("Failed to send Telegram message: %s", exc)
        return False


def register_bot_commands() -> None:
    """Register bot commands so they appear as a clickable menu in Telegram."""
    api_url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/setMyCommands"
    commands = [
        {"command": "list", "description": "📋 View your watchlist"},
        {"command": "remove", "description": "🗑️ Remove a product"},
        {"command": "edit", "description": "✏️ Change target price"},
        {"command": "history", "description": "📜 Price history for a product"},
        {"command": "pause", "description": "⏸️ Pause tracking"},
        {"command": "resume", "description": "▶️ Resume tracking"},
        {"command": "status", "description": "📊 Quick summary"},
        {"command": "news", "description": "📰 AI news summary"},
        {"command": "help", "description": "❓ Show all commands"},
    ]
    try:
        resp = requests.post(api_url, json={"commands": commands}, timeout=10)
        if resp.ok:
            log.info("✅ Bot menu commands registered.")
        else:
            log.warning("Could not register bot commands: %s", resp.text)
    except requests.RequestException as exc:
        log.warning("Could not register bot commands: %s", exc)


# ──────────────────────── Command Parsing ─────────────────────────

def detect_url_in_text(text: str) -> tuple[str, float | None] | None:
    """
    Smart URL detection — works with or without /add prefix.
    Supports:
      - Just a URL:              https://flipkart.com/...
      - URL + price:             https://flipkart.com/... 2000
      - /add URL:                /add https://flipkart.com/...
      - /add URL price:          /add https://flipkart.com/... 2000
    Returns (url, target_price_or_None) or None if no URL found.
    """
    cleaned = text.strip()
    # Remove /add prefix if present
    if cleaned.lower().startswith("/add"):
        cleaned = cleaned[4:].strip()

    # Find a URL in the text
    url_match = re.search(r"(https?://\S+)", cleaned)
    if not url_match:
        return None

    url = url_match.group(1)

    # Only accept Amazon/Flipkart URLs
    if detect_platform(url) == "unknown":
        return None

    # Look for a price number after the URL
    after_url = cleaned[url_match.end():].strip()
    price_match = re.match(r"(\d+\.?\d*)", after_url)
    target_price = float(price_match.group(1)) if price_match else None

    return url, target_price


def parse_remove_command(text: str) -> str | None:
    """Parse '/remove <n>' or '/remove all'. Returns the argument or None."""
    match = re.match(r"/remove\s+(\S+)", text.strip(), re.IGNORECASE)
    return match.group(1) if match else None


def parse_edit_command(text: str) -> tuple[int, float] | None:
    """Parse '/edit <n> <new_price>'. Returns (index, new_price) or None."""
    match = re.match(r"/edit\s+(\d+)\s+(\d+\.?\d*)", text.strip(), re.IGNORECASE)
    if match:
        return int(match.group(1)), float(match.group(2))
    return None


def parse_history_command(text: str) -> int | None:
    """Parse '/history <n>'. Returns the index or None."""
    match = re.match(r"/history\s+(\d+)", text.strip(), re.IGNORECASE)
    return int(match.group(1)) if match else None


def parse_pause_command(text: str) -> int | None:
    """Parse '/pause <n>'. Returns the index or None."""
    match = re.match(r"/pause\s+(\d+)", text.strip(), re.IGNORECASE)
    return int(match.group(1)) if match else None


def parse_resume_command(text: str) -> int | None:
    """Parse '/resume <n>'. Returns the index or None."""
    match = re.match(r"/resume\s+(\d+)", text.strip(), re.IGNORECASE)
    return int(match.group(1)) if match else None


def is_duplicate_url(products_ws: gspread.Worksheet, url: str) -> bool:
    """Check if a URL is already being tracked."""
    all_rows = products_ws.get_all_values()
    for row in all_rows[1:]:
        if len(row) > 1 and row[1].split("?")[0] == url.split("?")[0]:
            return True
    return False


# ──────────────────────── Add Product Logic ───────────────────────

def handle_add_product(
    products_ws: gspread.Worksheet,
    url: str,
    target_price: float | None,
) -> str:
    """
    Add a product to the watchlist. Returns a confirmation message.
    If target_price is None, auto-sets to 15% below current price.
    """
    # Check for duplicate
    if is_duplicate_url(products_ws, url):
        return "⚠️ This product is already in your watchlist."

    # Scrape the product
    info = scrape_product_info(url)
    if info and info.get("title"):
        name = info["title"]
        if len(name) > 60:
            name = name[:57] + "..."
    else:
        name = f"Product ({detect_platform(url).capitalize()})"

    platform = detect_platform(url).capitalize()
    current_price = info["price"] if info else None

    # Auto-calculate target if not provided
    if target_price is None and current_price:
        target_price = round(current_price * 0.85)  # 15% below current
        auto_label = " (auto: 15% below)"
    elif target_price is None:
        target_price = 0
        auto_label = " (set manually later)"
    else:
        auto_label = ""

    price_str = f"₹{current_price:,.2f}" if current_price else "N/A"

    # Append to Google Sheet
    products_ws.append_row(
        [name, url, str(target_price), str(current_price or "N/A")]
    )
    log.info("   ✅ Added '%s' to sheet.", name)

    # Build rich confirmation
    gap = ""
    if current_price and target_price:
        diff = current_price - target_price
        if diff > 0:
            gap = f"\n   📏 Gap to target: ₹{diff:,.0f} ({diff/current_price*100:.0f}% away)"
        else:
            gap = f"\n   🔥 Already below target by ₹{abs(diff):,.0f}!"

    now_str = now_ist().strftime("%H:%M IST, %d %b %Y")
    return (
        f"✅ <b>{name}</b>\n"
        f"   🏪 {platform}\n"
        f"   💰 Current: {price_str}\n"
        f"   🎯 Target: ₹{target_price:,.0f}{auto_label}"
        f"{gap}\n"
        f"   🕐 Tracking started: {now_str}"
    )


# ──────────────────────── News Logic ───────────────────────

# Category presets — shortcuts for common topics
CATEGORY_PRESETS = {
    "tech": "Technology",
    "sports": "Sports India",
    "business": "Business Finance India",
    "world": "World News",
    "entertainment": "Bollywood Entertainment",
    "science": "Science Discoveries",
    "health": "Health Wellness",
    "gaming": "Video Games Gaming",
    "crypto": "Cryptocurrency Bitcoin",
    "ai": "Artificial Intelligence AI",
}

# Supported languages for summaries
LANGUAGES = {
    "hindi", "tamil", "telugu", "bengali", "marathi", "gujarati",
    "kannada", "malayalam", "punjabi", "urdu", "spanish", "french",
    "german", "japanese", "korean", "chinese", "arabic",
}


def parse_news_args(raw_topic: str) -> tuple[str, str, bool]:
    """
    Parse the /news arguments into (topic, language, is_detail).

    Supports:
      /news                       → ("Technology", "English", False)
      /news Cricket               → ("Cricket", "English", False)
      /news Cricket hindi         → ("Cricket", "Hindi", False)
      /news detail Cricket        → ("Cricket", "English", True)
      /news detail Cricket hindi  → ("Cricket", "Hindi", True)
      /news tech                  → ("Technology", "English", False)
    """
    parts = raw_topic.strip().split()
    is_detail = False
    language = "English"

    if not parts:
        return "Technology", language, is_detail

    # Check for 'detail' flag
    if parts[0].lower() == "detail":
        is_detail = True
        parts = parts[1:]

    if not parts:
        return "Technology", language, is_detail

    # Check if last word is a language
    if len(parts) > 1 and parts[-1].lower() in LANGUAGES:
        language = parts[-1].capitalize()
        parts = parts[:-1]

    # Join remaining as topic
    topic = " ".join(parts)

    # Check for category preset
    if topic.lower() in CATEGORY_PRESETS:
        topic = CATEGORY_PRESETS[topic.lower()]

    return topic or "Technology", language, is_detail


def handle_news_command(raw_topic: str, chat_id: str) -> None:
    """Fetch Google News RSS for a topic and summarize with Gemini."""
    if not GEMINI_API_KEY:
        send_telegram_message(
            "⚠️ News feature is not configured. "
            "Set the GEMINI_API_KEY environment variable.", chat_id)
        return

    # Parse arguments
    topic, language, is_detail = parse_news_args(raw_topic)
    word_limit = 300 if is_detail else 100
    mode_label = "📖 Detail" if is_detail else "⚡ Quick"

    log.info("📰 News request: topic='%s', lang='%s', detail=%s", topic, language, is_detail)

    # 1. Fetch RSS
    from urllib.parse import quote
    rss_url = f"https://news.google.com/rss/search?q={quote(topic)}&hl=en-IN&gl=IN&ceid=IN:en"

    try:
        feed = feedparser.parse(rss_url)
    except Exception as exc:
        log.error("RSS fetch error: %s", exc)
        send_telegram_message("❌ Failed to fetch news.", chat_id)
        return

    entries = feed.entries[:5]
    if not entries:
        send_telegram_message(f"📰 No news found for <b>{topic}</b>.", chat_id)
        return

    headlines = "\n".join(f"- {e.title}" for e in entries)

    # 2. Summarize with Gemini
    genai.configure(api_key=GEMINI_API_KEY)
    model = genai.GenerativeModel("gemini-2.0-flash")

    lang_instruction = (
        f" Respond entirely in {language}." if language != "English" else ""
    )

    prompt = (
        f"You are a fun news anchor. Summarize these headlines about "
        f"'{topic}' in under {word_limit} words. Use emojis. Keep it casual and "
        f"informative. Do not include introductory phrases like 'Here is the summary'."
        f" Ignore duplicate or clickbait headlines — only summarize genuinely distinct stories."
        f"{lang_instruction}\n\n{headlines}"
    )

    try:
        response = model.generate_content(prompt)
        summary = response.text.strip()
    except Exception as exc:
        log.error("Gemini API error: %s", exc)
        summary = f"⚠️ AI summary failed. Here are the headlines:\n\n{headlines}"

    # 3. Build source links
    links = "\n".join(
        f'  • <a href="{e.link}">{e.title[:50]}{"…" if len(e.title) > 50 else ""}</a>'
        for e in entries
    )

    # 4. News-to-Price bridge — check if topic relates to any tracked product
    bridge_note = ""
    try:
        if GOOGLE_CREDENTIALS and SHEET_ID:
            sheet = connect_to_sheet()
            products_ws = get_products_worksheet(sheet)
            rows = products_ws.get_all_values()[1:]  # skip header
            topic_lower = topic.lower()
            for row in rows:
                if len(row) >= 4 and row[0]:
                    product_name = row[0].lower()
                    if (topic_lower in product_name or
                            any(w in product_name for w in topic_lower.split() if len(w) > 3)):
                        price_str = row[3] if row[3] else "N/A"
                        bridge_note += f"\n📌 <i>Related: You're tracking <b>{row[0]}</b> (₹{price_str})</i>"
    except Exception as exc:
        log.debug("News-to-price bridge skipped: %s", exc)

    # 5. Check & save news history (dedup)
    history_note = ""
    try:
        if GOOGLE_CREDENTIALS and SHEET_ID:
            import hashlib
            headlines_hash = hashlib.md5(headlines.encode()).hexdigest()[:12]
            sheet = connect_to_sheet()
            news_hist_ws = get_news_history_worksheet(sheet)
            existing = news_hist_ws.get_all_values()[1:]  # skip header
            # Check if same hash exists for this topic in last 10 entries
            for row in reversed(existing[-10:]):
                if len(row) >= 3 and row[1] == topic and row[2] == headlines_hash:
                    history_note = "\n\n🔁 <i>Same headlines as your last check — no new updates.</i>"
                    break
            if not history_note:
                now_str = now_ist().strftime("%Y-%m-%d %H:%M")
                news_hist_ws.append_row([now_str, topic, headlines_hash])
    except Exception as exc:
        log.debug("News history check skipped: %s", exc)

    # 6. Send
    lang_tag = f" ({language})" if language != "English" else ""
    send_telegram_message(
        f"📰 <b>News: {topic}</b>{lang_tag}  [{mode_label}]\n\n"
        f"{summary}\n\n"
        f"🔗 <b>Sources</b>\n{links}"
        f"{bridge_note}{history_note}\n\n"
        f"— <i>Powered by Gemini ✨</i>", chat_id)


def get_news_topics_worksheet(sheet: gspread.Spreadsheet) -> gspread.Worksheet:
    """Get or create the 'News_Topics' tab for saved topics."""
    try:
        ws = sheet.worksheet("News_Topics")
    except gspread.WorksheetNotFound:
        ws = sheet.add_worksheet(title="News_Topics", rows=50, cols=2)
        ws.update("A1:B1", [["Topic", "Added_Date"]])
        log.info("Created 'News_Topics' tab.")
    return ws


def get_news_history_worksheet(sheet: gspread.Spreadsheet) -> gspread.Worksheet:
    """Get or create the 'News_History' tab for dedup tracking."""
    try:
        ws = sheet.worksheet("News_History")
    except gspread.WorksheetNotFound:
        ws = sheet.add_worksheet(title="News_History", rows=500, cols=3)
        ws.update("A1:C1", [["Date", "Topic", "Headlines_Hash"]])
        log.info("Created 'News_History' tab.")
    return ws


def handle_news_save(topic: str, chat_id: str) -> None:
    """Save a topic to the News_Topics sheet."""
    if not topic.strip():
        send_telegram_message("⚠️ Usage: <code>/news save Cricket</code>", chat_id)
        return
    try:
        sheet = connect_to_sheet()
        ws = get_news_topics_worksheet(sheet)
        # Check for duplicates
        existing = ws.col_values(1)[1:]  # skip header
        if topic.strip().lower() in [t.lower() for t in existing]:
            send_telegram_message(f"ℹ️ <b>{topic.strip()}</b> is already saved.", chat_id)
            return
        now_str = now_ist().strftime("%Y-%m-%d %H:%M")
        ws.append_row([topic.strip(), now_str])
        send_telegram_message(f"✅ Saved topic: <b>{topic.strip()}</b>\n\nUse /news saved to get all your saved topics.", chat_id)
    except Exception as exc:
        log.error("Failed to save news topic: %s", exc)
        send_telegram_message("❌ Failed to save topic.", chat_id)


def handle_news_saved(chat_id: str) -> None:
    """Fetch news for all saved topics."""
    try:
        sheet = connect_to_sheet()
        ws = get_news_topics_worksheet(sheet)
        topics = ws.col_values(1)[1:]  # skip header
    except Exception as exc:
        log.error("Failed to read saved topics: %s", exc)
        send_telegram_message("❌ Failed to read saved topics.", chat_id)
        return

    if not topics:
        send_telegram_message("📭 No saved topics yet.\n\nUse <code>/news save Cricket</code> to add one.", chat_id)
        return

    send_telegram_message(f"📰 Fetching news for {len(topics)} saved topics...", chat_id)
    for t in topics:
        handle_news_command(t, chat_id)


# ──────────────────────── Wave 3: Multi-Source + Deep Search ───────────────────────

def fetch_multi_source_news(topic: str, max_per_source: int = 3) -> list[dict]:
    """Fetch news from multiple RSS sources and merge results."""
    from urllib.parse import quote
    sources = {
        "Google": f"https://news.google.com/rss/search?q={quote(topic)}&hl=en-IN&gl=IN&ceid=IN:en",
        "Reddit": f"https://www.reddit.com/r/{quote(topic)}/hot/.rss?limit=5",
        "HN": f"https://hnrss.org/newest?q={quote(topic)}&count=5",
    }
    all_entries = []
    for source_name, url in sources.items():
        try:
            feed = feedparser.parse(url)
            for entry in feed.entries[:max_per_source]:
                all_entries.append({
                    "title": entry.get("title", "Untitled"),
                    "link": entry.get("link", ""),
                    "source": source_name,
                })
        except Exception as exc:
            log.warning("RSS fetch failed for %s: %s", source_name, exc)

    # Deduplicate by rough title similarity (first 30 chars lowercase)
    seen = set()
    unique = []
    for e in all_entries:
        key = e["title"][:30].lower().strip()
        if key not in seen:
            seen.add(key)
            unique.append(e)
    return unique[:7]  # top 7 across sources


def handle_news_multi(raw_topic: str, chat_id: str) -> None:
    """Fetch news from multiple sources and summarize."""
    if not GEMINI_API_KEY:
        send_telegram_message(
            "⚠️ News feature is not configured. "
            "Set the GEMINI_API_KEY environment variable.", chat_id)
        return

    topic = raw_topic.strip() or "Technology"
    log.info("📰 Multi-source news: topic='%s'", topic)

    entries = fetch_multi_source_news(topic)
    if not entries:
        send_telegram_message(f"📰 No news found for <b>{topic}</b> across sources.", chat_id)
        return

    headlines = "\n".join(f"- [{e['source']}] {e['title']}" for e in entries)

    genai.configure(api_key=GEMINI_API_KEY)
    model = genai.GenerativeModel("gemini-2.0-flash")
    prompt = (
        f"You are a fun news anchor. Summarize these headlines from multiple sources about "
        f"'{topic}' in under 150 words. Use emojis. Mention which sources reported what. "
        f"Ignore duplicates and clickbait.\n\n{headlines}"
    )

    try:
        summary = model.generate_content(prompt).text.strip()
    except Exception as exc:
        log.error("Gemini API error: %s", exc)
        summary = f"⚠️ AI summary failed.\n\n{headlines}"

    links = "\n".join(
        f'  • [{e["source"]}] <a href="{e["link"]}">{e["title"][:45]}{"…" if len(e["title"]) > 45 else ""}</a>'
        for e in entries
    )

    send_telegram_message(
        f"🌐 <b>Multi-Source News: {topic}</b>\n\n"
        f"{summary}\n\n"
        f"🔗 <b>Sources</b>\n{links}\n\n"
        f"— <i>Powered by Gemini ✨</i>", chat_id)


def handle_news_trending(chat_id: str) -> None:
    """Fetch trending topics from Google Trends and summarize."""
    if not GEMINI_API_KEY:
        send_telegram_message("⚠️ Set GEMINI_API_KEY first.", chat_id)
        return

    log.info("📰 Trending topics request")
    rss_url = "https://trends.google.com/trending/rss?geo=IN"

    try:
        feed = feedparser.parse(rss_url)
    except Exception as exc:
        log.error("Trends RSS error: %s", exc)
        send_telegram_message("❌ Failed to fetch trending topics.", chat_id)
        return

    entries = feed.entries[:7]
    if not entries:
        send_telegram_message("📰 No trending topics found.", chat_id)
        return

    topics_list = "\n".join(f"- {e.title}" for e in entries)

    genai.configure(api_key=GEMINI_API_KEY)
    model = genai.GenerativeModel("gemini-2.0-flash")
    prompt = (
        f"You are a fun news anchor. Here are today's trending topics in India. "
        f"Give a brief, exciting 1-line description for each. Use emojis. "
        f"Keep total response under 150 words.\n\n{topics_list}"
    )

    try:
        summary = model.generate_content(prompt).text.strip()
    except Exception as exc:
        log.error("Gemini API error: %s", exc)
        summary = topics_list

    send_telegram_message(
        f"🔥 <b>Trending Now (India)</b>\n\n{summary}\n\n"
        f"— <i>Powered by Gemini ✨</i>", chat_id)


def handle_news_deep(raw_topic: str, chat_id: str) -> None:
    """Deep-read the top article and provide an in-depth summary."""
    if not GEMINI_API_KEY:
        send_telegram_message("⚠️ Set GEMINI_API_KEY first.", chat_id)
        return

    topic = raw_topic.strip() or "Technology"
    log.info("📰 Deep search: topic='%s'", topic)

    # Fetch top article from Google News
    from urllib.parse import quote
    rss_url = f"https://news.google.com/rss/search?q={quote(topic)}&hl=en-IN&gl=IN&ceid=IN:en"

    try:
        feed = feedparser.parse(rss_url)
    except Exception as exc:
        log.error("RSS fetch error: %s", exc)
        send_telegram_message("❌ Failed to fetch news.", chat_id)
        return

    if not feed.entries:
        send_telegram_message(f"📰 No articles found for <b>{topic}</b>.", chat_id)
        return

    top_entry = feed.entries[0]
    article_url = top_entry.link
    article_title = top_entry.title

    send_telegram_message(f"🔍 Reading full article: <b>{article_title[:60]}</b>...", chat_id)

    # Extract full article text
    try:
        import trafilatura
        downloaded = trafilatura.fetch_url(article_url)
        article_text = trafilatura.extract(downloaded) if downloaded else None
    except Exception as exc:
        log.error("Article extraction error: %s", exc)
        article_text = None

    if not article_text:
        send_telegram_message("⚠️ Could not read the full article. Falling back to headline summary.", chat_id)
        handle_news_command(topic, chat_id)
        return

    # Truncate to 3000 chars for Gemini
    article_text = article_text[:3000]

    genai.configure(api_key=GEMINI_API_KEY)
    model = genai.GenerativeModel("gemini-2.0-flash")
    prompt = (
        f"You are an expert journalist. Provide a detailed summary of this article about '{topic}'. "
        f"Cover: key facts, who is involved, why it matters, and what happens next. "
        f"Use emojis. 200-300 words. Do not start with 'Here is a summary'.\n\n"
        f"Title: {article_title}\n\nArticle:\n{article_text}"
    )

    try:
        summary = model.generate_content(prompt).text.strip()
    except Exception as exc:
        log.error("Gemini API error: %s", exc)
        summary = f"⚠️ AI summary failed.\n\n{article_text[:500]}..."

    send_telegram_message(
        f"📖 <b>Deep Dive: {article_title[:50]}</b>\n\n"
        f"{summary}\n\n"
        f'🔗 <a href="{article_url}">Read full article</a>\n\n'
        f"— <i>Powered by Gemini ✨</i>", chat_id)


# ══════════════════════════ THREE PHASES ══════════════════════════


def phase1_process_commands(
    settings_ws: gspread.Worksheet,
    products_ws: gspread.Worksheet,
    history_ws: gspread.Worksheet | None = None,
) -> list[str]:
    """
    Phase 1: Process Telegram commands and auto-detected URLs.
    Returns a list of confirmation messages for newly added products.
    """
    log.info("═══ Phase 1: Processing Telegram Commands ═══")

    last_update_id = get_last_update_id(settings_ws)
    updates = get_telegram_updates(last_update_id)

    if not updates:
        log.info("No new Telegram messages.")
        return []

    added_messages: list[str] = []
    new_last_id = last_update_id

    for update in updates:
        update_id = update.get("update_id", 0)
        new_last_id = max(new_last_id, update_id)

        message = update.get("message", {})
        text = message.get("text", "")
        chat_id = str(message.get("chat", {}).get("id", CHAT_ID))

        if not text:
            continue

        text_lower = text.strip().lower()

        # ── /news — AI News Summary ──
        if text_lower.startswith("/news"):
            args = text[5:].strip()
            if text_lower.startswith("/news saved"):
                handle_news_saved(chat_id)
            elif text_lower.startswith("/news save"):
                handle_news_save(args[5:].strip(), chat_id)
            elif text_lower.startswith("/news trending"):
                handle_news_trending(chat_id)
            elif text_lower.startswith("/news multi"):
                handle_news_multi(args[6:].strip(), chat_id)
            elif text_lower.startswith("/news deep"):
                handle_news_deep(args[5:].strip(), chat_id)
            else:
                handle_news_command(args, chat_id)
            continue

        # ── /start — Welcome message ──
        if text_lower.startswith("/start"):
            send_telegram_message(
                "🎯 <b>Welcome to Price Drop Hunter!</b>\n\n"
                "I track prices on <b>Amazon</b> and <b>Flipkart</b> "
                "and alert you when they drop.\n\n"
                "<b>How to add a product:</b>\n"
                "Just paste any Amazon/Flipkart URL!\n\n"
                "• <code>URL</code> — auto-target 15% below current price\n"
                "• <code>URL 2000</code> — set ₹2,000 as your target\n\n"
                "Type /help to see all commands.",
                chat_id,
            )
            continue

        # ── /help — Show all commands ──
        if text_lower.startswith("/help"):
            send_telegram_message(
                "❓ <b>Available Commands</b>\n\n"
                "<b>Add products:</b>\n"
                "• Just paste any Flipkart/Amazon URL\n"
                "• Add a target price after the URL\n"
                "• Or use: <code>/add URL PRICE</code>\n\n"
                "<b>Manage watchlist:</b>\n"
                "• /list — View all tracked products\n"
                "• /remove 2 — Remove product #2\n"
                "• /remove all — Clear entire watchlist\n"
                "• /edit 2 1500 — Change target for #2\n"
                "• /pause 2 — Pause tracking for #2\n"
                "• /resume 2 — Resume tracking for #2\n\n"
                "<b>Info:</b>\n"
                "• /history 1 — Price history for #1\n"
                "• /status — Quick summary\n"
                "• /news <topic> — AI news (try: tech, sports, detail)\n"
                "• /news save <topic> / saved — Save & fetch topics\n"
                "• /news multi / trending / deep — Advanced modes\n"
                "• /help — This message\n\n"
                "Prices are checked every hour automatically. 🕐",
                chat_id,
            )
            continue

        # ── /status — Quick summary ──
        if text_lower.startswith("/status"):
            all_rows = products_ws.get_all_values()
            count = max(0, len(all_rows) - 1)
            paused = sum(1 for r in all_rows[1:] if len(r) > 5 and r[5] == "paused")
            active = count - paused
            send_telegram_message(
                f"📊 <b>Status</b>\n\n"
                f"Products tracked: <b>{count}</b> "
                f"(🟢 {active} active, ⏸️ {paused} paused)\n"
                f"Price checks: every 1 hour\n"
                f"Scraping: Amazon + Flipkart",
                chat_id,
            )
            continue

        # ── /list — View watchlist ──
        if text_lower.startswith("/list"):
            log.info("📋 /list command received.")
            all_rows = products_ws.get_all_values()
            if len(all_rows) <= 1:
                send_telegram_message(
                    "📋 Your watchlist is empty.\n"
                    "Just paste any Amazon/Flipkart URL to start tracking!",
                    chat_id,
                )
            else:
                lines = ["📋 <b>Your Watchlist</b>\n"]
                for i, row in enumerate(all_rows[1:], 1):
                    name = row[0] if len(row) > 0 else "?"
                    target = row[2] if len(row) > 2 else "?"
                    current = row[3] if len(row) > 3 else "N/A"
                    status = row[5] if len(row) > 5 else "active"
                    icon = "⏸️" if status == "paused" else "🟢"
                    lines.append(
                        f"{icon} {i}. <b>{name}</b>\n"
                        f"   Target: ₹{target} | Last: {current}"
                    )
                send_telegram_message("\n".join(lines), chat_id)
            continue

        # ── /remove — Remove product(s) ──
        if text_lower.strip() == "/remove" or text_lower.strip().startswith("/remove@"):
            # Bare command — show watchlist + usage
            all_rows = products_ws.get_all_values()
            if len(all_rows) <= 1:
                send_telegram_message("📋 Watchlist is empty — nothing to remove.", chat_id)
            else:
                lines = ["🗑️ <b>Which product to remove?</b>\n"]
                for i, row in enumerate(all_rows[1:], 1):
                    name = row[0] if len(row) > 0 else "?"
                    lines.append(f"{i}. {name}")
                lines.append("\nReply with:\n• <code>/remove 2</code> — remove #2\n• <code>/remove all</code> — clear all")
                send_telegram_message("\n".join(lines), chat_id)
            continue

        remove_arg = parse_remove_command(text)
        if remove_arg is not None:
            all_rows = products_ws.get_all_values()
            data_count = len(all_rows) - 1

            if remove_arg.lower() == "all":
                if data_count <= 0:
                    send_telegram_message("📋 Watchlist is already empty.", chat_id)
                else:
                    # Delete all data rows (keep header)
                    for row_idx in range(data_count + 1, 1, -1):
                        products_ws.delete_rows(row_idx)
                    send_telegram_message(
                        f"🗑️ Cleared <b>{data_count}</b> product(s) from your watchlist.",
                        chat_id,
                    )
                    log.info("   🗑️ Cleared all %d products.", data_count)
                continue

            try:
                idx = int(remove_arg)
            except ValueError:
                send_telegram_message(
                    "⚠️ Usage: <code>/remove 2</code> or <code>/remove all</code>",
                    chat_id,
                )
                continue

            if idx < 1 or idx > data_count:
                send_telegram_message(
                    f"⚠️ Invalid number. You have {data_count} product(s). "
                    f"Use /list to see them.",
                    chat_id,
                )
            else:
                removed_name = all_rows[idx][0] if len(all_rows[idx]) > 0 else "?"
                products_ws.delete_rows(idx + 1)  # +1 for header
                send_telegram_message(
                    f"🗑️ Removed <b>{removed_name}</b> from your watchlist.",
                    chat_id,
                )
                log.info("   🗑️ Removed row %d: '%s'", idx, removed_name)
            continue

        # ── /edit — Change target price ──
        if text_lower.strip() == "/edit" or text_lower.strip().startswith("/edit@"):
            # Bare command — show watchlist + usage
            all_rows = products_ws.get_all_values()
            if len(all_rows) <= 1:
                send_telegram_message("📋 Watchlist is empty — nothing to edit.", chat_id)
            else:
                lines = ["✏️ <b>Which product to edit?</b>\n"]
                for i, row in enumerate(all_rows[1:], 1):
                    name = row[0] if len(row) > 0 else "?"
                    target = row[2] if len(row) > 2 else "?"
                    lines.append(f"{i}. {name} (target: ₹{target})")
                lines.append("\nReply with:\n• <code>/edit 2 1500</code> — set #2 target to ₹1,500")
                send_telegram_message("\n".join(lines), chat_id)
            continue

        edit_parsed = parse_edit_command(text)
        if edit_parsed:
            idx, new_price = edit_parsed
            all_rows = products_ws.get_all_values()
            data_count = len(all_rows) - 1

            if idx < 1 or idx > data_count:
                send_telegram_message(
                    f"⚠️ Invalid number. You have {data_count} product(s). "
                    f"Use /list to see them.",
                    chat_id,
                )
            else:
                name = all_rows[idx][0] if len(all_rows[idx]) > 0 else "?"
                products_ws.update_cell(idx + 1, 3, str(new_price))  # +1 for header
                send_telegram_message(
                    f"✏️ Updated target for <b>{name}</b> to ₹{new_price:,.0f}",
                    chat_id,
                )
                log.info("   ✏️ Updated target for '%s' → ₹%.0f", name, new_price)
            continue

        # ── /history — Show price history ──
        if text_lower.strip() == "/history" or text_lower.strip().startswith("/history@"):
            all_rows = products_ws.get_all_values()
            if len(all_rows) <= 1:
                send_telegram_message("📋 Watchlist is empty — nothing to show.", chat_id)
            else:
                lines = ["📜 <b>Which product's history?</b>\n"]
                for i, row in enumerate(all_rows[1:], 1):
                    name = row[0] if len(row) > 0 else "?"
                    lines.append(f"{i}. {name}")
                lines.append("\nReply with:\n• <code>/history 1</code> — show history for #1")
                send_telegram_message("\n".join(lines), chat_id)
            continue

        history_idx = parse_history_command(text)
        if history_idx is not None:
            all_rows = products_ws.get_all_values()
            data_count = len(all_rows) - 1
            if history_idx < 1 or history_idx > data_count:
                send_telegram_message(
                    f"⚠️ Invalid number. You have {data_count} product(s). "
                    f"Use /list to see them.", chat_id,
                )
            elif history_ws:
                product_name = all_rows[history_idx][0]
                hist_rows = history_ws.get_all_values()
                matches = [r for r in hist_rows[1:] if r[1] == product_name]
                if not matches:
                    send_telegram_message(
                        f"📜 No history yet for <b>{product_name}</b>.\n"
                        "History is recorded during hourly price checks.",
                        chat_id,
                    )
                else:
                    recent = matches[-10:]  # Last 10 entries
                    lines = [f"📜 <b>Price History: {product_name}</b>\n"]
                    for entry in reversed(recent):
                        date = entry[0] if len(entry) > 0 else "?"
                        price = entry[2] if len(entry) > 2 else "?"
                        lines.append(f"  {date} — ₹{price}")
                    send_telegram_message("\n".join(lines), chat_id)
            else:
                send_telegram_message("⚠️ Price history is not available.", chat_id)
            continue

        # ── /pause — Pause tracking ──
        if text_lower.strip() == "/pause" or text_lower.strip().startswith("/pause@"):
            all_rows = products_ws.get_all_values()
            if len(all_rows) <= 1:
                send_telegram_message("📋 Watchlist is empty.", chat_id)
            else:
                lines = ["⏸️ <b>Which product to pause?</b>\n"]
                for i, row in enumerate(all_rows[1:], 1):
                    name = row[0] if len(row) > 0 else "?"
                    status = row[5] if len(row) > 5 else "active"
                    icon = "⏸️" if status == "paused" else "🟢"
                    lines.append(f"{icon} {i}. {name}")
                lines.append("\nReply with:\n• <code>/pause 1</code> — pause #1")
                send_telegram_message("\n".join(lines), chat_id)
            continue

        pause_idx = parse_pause_command(text)
        if pause_idx is not None:
            all_rows = products_ws.get_all_values()
            data_count = len(all_rows) - 1
            if pause_idx < 1 or pause_idx > data_count:
                send_telegram_message(
                    f"⚠️ Invalid number. You have {data_count} product(s).", chat_id,
                )
            else:
                name = all_rows[pause_idx][0] if len(all_rows[pause_idx]) > 0 else "?"
                products_ws.update_cell(pause_idx + 1, 6, "paused")
                send_telegram_message(
                    f"⏸️ Paused tracking for <b>{name}</b>.\n"
                    f"Use <code>/resume {pause_idx}</code> to resume.",
                    chat_id,
                )
                log.info("   ⏸️ Paused '%s'.", name)
            continue

        # ── /resume — Resume tracking ──
        if text_lower.strip() == "/resume" or text_lower.strip().startswith("/resume@"):
            all_rows = products_ws.get_all_values()
            if len(all_rows) <= 1:
                send_telegram_message("📋 Watchlist is empty.", chat_id)
            else:
                lines = ["▶️ <b>Which product to resume?</b>\n"]
                for i, row in enumerate(all_rows[1:], 1):
                    name = row[0] if len(row) > 0 else "?"
                    status = row[5] if len(row) > 5 else "active"
                    icon = "⏸️" if status == "paused" else "🟢"
                    lines.append(f"{icon} {i}. {name}")
                lines.append("\nReply with:\n• <code>/resume 1</code> — resume #1")
                send_telegram_message("\n".join(lines), chat_id)
            continue

        resume_idx = parse_resume_command(text)
        if resume_idx is not None:
            all_rows = products_ws.get_all_values()
            data_count = len(all_rows) - 1
            if resume_idx < 1 or resume_idx > data_count:
                send_telegram_message(
                    f"⚠️ Invalid number. You have {data_count} product(s).", chat_id,
                )
            else:
                name = all_rows[resume_idx][0] if len(all_rows[resume_idx]) > 0 else "?"
                products_ws.update_cell(resume_idx + 1, 6, "active")
                send_telegram_message(
                    f"▶️ Resumed tracking for <b>{name}</b>.",
                    chat_id,
                )
                log.info("   ▶️ Resumed '%s'.", name)
            continue

        # ── Auto-detect URL (no /add needed) ──
        url_detected = detect_url_in_text(text)
        if url_detected:
            url, target_price = url_detected
            log.info("📥 URL detected: %s (target: %s)", url, target_price or "auto")
            msg = handle_add_product(products_ws, url, target_price)
            added_messages.append(msg)
            send_telegram_message(msg, chat_id)
            continue

    # Update the last processed ID
    if new_last_id > last_update_id:
        set_last_update_id(settings_ws, new_last_id)
        log.info("Updated last_update_id → %d", new_last_id)

    return added_messages


def phase2_check_prices(
    products_ws: gspread.Worksheet,
    history_ws: gspread.Worksheet | None = None,
) -> tuple[list[dict], list[dict]]:
    """
    Phase 2: Read all products from the sheet, scrape live prices,
    update the Current_Price column, log history, and return
    (alerts, changes).
    """
    log.info("═══ Phase 2: Checking Live Prices ═══")

    all_rows = products_ws.get_all_values()
    if len(all_rows) <= 1:
        log.info("No products in the sheet.")
        return [], []

    alerts: list[dict] = []
    changes: list[dict] = []  # All price movements
    checked = 0

    for i, row in enumerate(all_rows[1:], 2):  # row 2 onwards (1-indexed in Sheets)
        if len(row) < 3:
            continue

        name = row[0]
        url = row[1]
        target = float(row[2])
        old_price_str = row[3] if len(row) > 3 else "N/A"
        last_alerted_str = row[4] if len(row) > 4 else ""
        status = row[5] if len(row) > 5 else "active"

        # Skip paused products
        if status == "paused":
            log.info("⏸️  [%d/%d] %s — paused, skipping.", i - 1, len(all_rows) - 1, name)
            continue

        # Parse old price for comparison
        try:
            old_price = float(old_price_str) if old_price_str != "N/A" else None
        except ValueError:
            old_price = None

        log.info("🔍 [%d/%d] %s", i - 1, len(all_rows) - 1, name)

        try:
            info = scrape_product_info(url)
        except Exception as exc:
            log.error("   ❌ Error scraping '%s': %s", name, exc)
            continue

        if not info or info.get("price") is None:
            log.warning("   ⚠️  Could not get price for '%s'.", name)
            continue

        live_price = info["price"]
        checked += 1
        log.info("   💰 ₹%.2f (target ₹%.0f)", live_price, target)

        # Update Current_Price in the sheet (column D)
        try:
            products_ws.update_cell(i, 4, f"{live_price:.2f}")
        except Exception as exc:
            log.warning("   Could not update sheet cell: %s", exc)

        # Log to Price_History
        if history_ws:
            log_price_history(history_ws, name, live_price, target)

        # Track price change
        if old_price is not None and old_price != live_price:
            diff = live_price - old_price
            changes.append({
                "name": name,
                "old_price": old_price,
                "new_price": live_price,
                "diff": diff,
                "target": target,
            })

        if live_price <= target:
            # Smart alert: skip if we already notified at this price
            if last_alerted_str == f"{live_price:.2f}":
                log.info("   🔕 Already alerted at ₹%.2f, skipping.", live_price)
            else:
                alerts.append({
                    "name": name,
                    "url": url,
                    "live_price": live_price,
                    "target_price": target,
                    "saved": target - live_price,
                    "row_index": i,  # For writing Last_Alerted
                })
                log.info("   🔥 DEAL! ₹%.0f below target.", target - live_price)
        else:
            # Price above target: clear Last_Alerted so future drops re-trigger
            if last_alerted_str:
                try:
                    products_ws.update_cell(i, 5, "")
                except Exception:
                    pass
            log.info("   ⏳ Above target by ₹%.0f.", live_price - target)

    log.info("   📊 Checked %d products, %d changes, %d deals.",
             checked, len(changes), len(alerts))
    return alerts, changes


def phase3_notify(
    added_messages: list[str],
    alerts: list[dict],
    changes: list[dict] | None = None,
    total_checked: int = 0,
    products_ws: gspread.Worksheet | None = None,
) -> None:
    """
    Phase 3: Send one consolidated Telegram message
    covering new additions, price-drop alerts, and price movements.
    """
    log.info("═══ Phase 3: Sending Notifications ═══")

    lines: list[str] = []

    # ── Newly added products ──
    if added_messages:
        lines.append("📥 <b>Newly Added to Watchlist</b>\n")
        lines.extend(added_messages)
        lines.append("")

    # ── Price drop alerts ──
    if alerts:
        lines.append("🔥 <b>Price Drop Alerts!</b>\n")
        for i, deal in enumerate(alerts, 1):
            lines.append(
                f"{i}. <b>{deal['name']}</b>\n"
                f"   💰 ₹{deal['live_price']:,.2f}  (target ₹{deal['target_price']:,.0f})\n"
                f"   📉 You save ₹{deal['saved']:,.2f}\n"
                f"   🔗 <a href=\"{deal['url']}\">Buy Now →</a>\n"
            )
            # Write Last_Alerted so we don't re-alert at the same price
            if products_ws and "row_index" in deal:
                try:
                    products_ws.update_cell(
                        deal["row_index"], 5, f"{deal['live_price']:.2f}"
                    )
                except Exception:
                    pass

    # ── Price movements (even if not deals) ──
    if changes:
        lines.append("📊 <b>Price Movements</b>\n")
        for ch in changes:
            diff = ch["diff"]
            if diff < 0:
                arrow = "📉"
                label = f"Dropped ₹{abs(diff):,.0f}"
            else:
                arrow = "📈"
                label = f"Rose ₹{diff:,.0f}"
            lines.append(
                f"{arrow} <b>{ch['name']}</b>\n"
                f"   ₹{ch['old_price']:,.0f} → ₹{ch['new_price']:,.0f} ({label})"
            )
        lines.append("")

    # ── Summary footer ──
    if total_checked > 0:
        unchanged = total_checked - len(changes or []) - len(alerts)
        if unchanged > 0 and not changes and not alerts:
            lines.append(f"✅ All {total_checked} products checked — no changes.")
        elif unchanged > 0:
            lines.append(f"✅ {unchanged} other product(s) unchanged.")

    if not lines:
        log.info("Nothing to report. No Telegram message sent.")
        return

    now_str = now_ist().strftime("%H:%M IST")
    lines.append(f"\n— <i>Price Drop Hunter 🎯 ({now_str})</i>")
    send_telegram_message("\n".join(lines))


# ──────────────────── Webhook: Single Message Handler ────────────

def process_single_message(
    text: str,
    chat_id: str,
    products_ws: gspread.Worksheet,
    history_ws: gspread.Worksheet | None = None,
) -> None:
    """
    Process a single incoming Telegram message instantly.
    Used by the webhook endpoint for real-time responses.
    """
    if not text:
        return

    text_lower = text.strip().lower()

    # ── /news — AI News Summary ──
    if text_lower.startswith("/news"):
        args = text[5:].strip()
        if text_lower.startswith("/news saved"):
            handle_news_saved(chat_id)
        elif text_lower.startswith("/news save"):
            handle_news_save(args[5:].strip(), chat_id)
        elif text_lower.startswith("/news trending"):
            handle_news_trending(chat_id)
        elif text_lower.startswith("/news multi"):
            handle_news_multi(args[6:].strip(), chat_id)
        elif text_lower.startswith("/news deep"):
            handle_news_deep(args[5:].strip(), chat_id)
        else:
            handle_news_command(args, chat_id)
        return

    # ── /start — Welcome message ──
    if text_lower.startswith("/start"):
        send_telegram_message(
            "🎯 <b>Welcome to Price Drop Hunter!</b>\n\n"
            "I track prices on <b>Amazon</b> and <b>Flipkart</b> "
            "and alert you when they drop.\n\n"
            "<b>How to add a product:</b>\n"
            "Just paste any Amazon/Flipkart URL!\n\n"
            "• <code>URL</code> — auto-target 15% below current price\n"
            "• <code>URL 2000</code> — set ₹2,000 as your target\n\n"
            "Type /help to see all commands.",
            chat_id,
        )
        return

    # ── /help — Show all commands ──
    if text_lower.startswith("/help"):
        send_telegram_message(
            "❓ <b>Available Commands</b>\n\n"
            "<b>Add products:</b>\n"
            "• Just paste any Flipkart/Amazon URL\n"
            "• Add a target price after the URL\n"
            "• Or use: <code>/add URL PRICE</code>\n\n"
            "<b>Manage watchlist:</b>\n"
            "• /list — View all tracked products\n"
            "• /remove 2 — Remove product #2\n"
            "• /remove all — Clear entire watchlist\n"
            "• /edit 2 1500 — Change target for #2\n"
            "• /pause 2 — Pause tracking for #2\n"
            "• /resume 2 — Resume tracking for #2\n\n"
            "<b>Info:</b>\n"
            "• /history 1 — Price history for #1\n"
            "• /status — Quick summary\n"
            "• /news <topic> — AI news (try: tech, sports, detail)\n"
            "• /news save <topic> / saved — Save & fetch topics\n"
            "• /news multi / trending / deep — Advanced modes\n"
            "• /help — This message\n\n"
            "Prices are checked every hour automatically. 🕐",
            chat_id,
        )
        return

    # ── /status — Quick summary ──
    if text_lower.startswith("/status"):
        all_rows = products_ws.get_all_values()
        count = max(0, len(all_rows) - 1)
        paused = sum(1 for r in all_rows[1:] if len(r) > 5 and r[5] == "paused")
        active = count - paused
        send_telegram_message(
            f"📊 <b>Status</b>\n\n"
            f"Products tracked: <b>{count}</b> "
            f"(🟢 {active} active, ⏸️ {paused} paused)\n"
            f"Price checks: every 1 hour\n"
            f"Scraping: Amazon + Flipkart",
            chat_id,
        )
        return

    # ── /list — View watchlist ──
    if text_lower.startswith("/list"):
        log.info("📋 /list command received.")
        all_rows = products_ws.get_all_values()
        if len(all_rows) <= 1:
            send_telegram_message(
                "📋 Your watchlist is empty.\n"
                "Just paste any Amazon/Flipkart URL to start tracking!",
                chat_id,
            )
        else:
            lines = ["📋 <b>Your Watchlist</b>\n"]
            for i, row in enumerate(all_rows[1:], 1):
                name = row[0] if len(row) > 0 else "?"
                target = row[2] if len(row) > 2 else "?"
                current = row[3] if len(row) > 3 else "N/A"
                status = row[5] if len(row) > 5 else "active"
                icon = "⏸️" if status == "paused" else "🟢"
                lines.append(
                    f"{icon} {i}. <b>{name}</b>\n"
                    f"   Target: ₹{target} | Last: {current}"
                )
            send_telegram_message("\n".join(lines), chat_id)
        return

    # ── /remove — Remove product(s) ──
    if text_lower.strip() == "/remove" or text_lower.strip().startswith("/remove@"):
        all_rows = products_ws.get_all_values()
        if len(all_rows) <= 1:
            send_telegram_message("📋 Watchlist is empty — nothing to remove.", chat_id)
        else:
            lines = ["🗑️ <b>Which product to remove?</b>\n"]
            for i, row in enumerate(all_rows[1:], 1):
                name = row[0] if len(row) > 0 else "?"
                lines.append(f"{i}. {name}")
            lines.append("\nReply with:\n• <code>/remove 2</code> — remove #2\n• <code>/remove all</code> — clear all")
            send_telegram_message("\n".join(lines), chat_id)
        return

    remove_arg = parse_remove_command(text)
    if remove_arg is not None:
        all_rows = products_ws.get_all_values()
        data_count = len(all_rows) - 1

        if remove_arg.lower() == "all":
            if data_count <= 0:
                send_telegram_message("📋 Watchlist is already empty.", chat_id)
            else:
                for row_idx in range(data_count + 1, 1, -1):
                    products_ws.delete_rows(row_idx)
                send_telegram_message(
                    f"🗑️ Cleared <b>{data_count}</b> product(s) from your watchlist.",
                    chat_id,
                )
                log.info("   🗑️ Cleared all %d products.", data_count)
            return

        try:
            idx = int(remove_arg)
        except ValueError:
            send_telegram_message(
                "⚠️ Usage: <code>/remove 2</code> or <code>/remove all</code>",
                chat_id,
            )
            return

        if idx < 1 or idx > data_count:
            send_telegram_message(
                f"⚠️ Invalid number. You have {data_count} product(s). "
                f"Use /list to see them.",
                chat_id,
            )
        else:
            removed_name = all_rows[idx][0] if len(all_rows[idx]) > 0 else "?"
            products_ws.delete_rows(idx + 1)
            send_telegram_message(
                f"🗑️ Removed <b>{removed_name}</b> from your watchlist.",
                chat_id,
            )
            log.info("   🗑️ Removed row %d: '%s'", idx, removed_name)
        return

    # ── /edit — Change target price ──
    if text_lower.strip() == "/edit" or text_lower.strip().startswith("/edit@"):
        all_rows = products_ws.get_all_values()
        if len(all_rows) <= 1:
            send_telegram_message("📋 Watchlist is empty — nothing to edit.", chat_id)
        else:
            lines = ["✏️ <b>Which product to edit?</b>\n"]
            for i, row in enumerate(all_rows[1:], 1):
                name = row[0] if len(row) > 0 else "?"
                target = row[2] if len(row) > 2 else "?"
                lines.append(f"{i}. {name} (target: ₹{target})")
            lines.append("\nReply with:\n• <code>/edit 2 1500</code> — set #2 target to ₹1,500")
            send_telegram_message("\n".join(lines), chat_id)
        return

    edit_parsed = parse_edit_command(text)
    if edit_parsed:
        idx, new_price = edit_parsed
        all_rows = products_ws.get_all_values()
        data_count = len(all_rows) - 1

        if idx < 1 or idx > data_count:
            send_telegram_message(
                f"⚠️ Invalid number. You have {data_count} product(s). "
                f"Use /list to see them.",
                chat_id,
            )
        else:
            name = all_rows[idx][0] if len(all_rows[idx]) > 0 else "?"
            products_ws.update_cell(idx + 1, 3, str(new_price))
            send_telegram_message(
                f"✏️ Updated target for <b>{name}</b> to ₹{new_price:,.0f}",
                chat_id,
            )
            log.info("   ✏️ Updated target for '%s' → ₹%.0f", name, new_price)
        return

    # ── /history — Show price history ──
    if text_lower.strip() == "/history" or text_lower.strip().startswith("/history@"):
        all_rows = products_ws.get_all_values()
        if len(all_rows) <= 1:
            send_telegram_message("📋 Watchlist is empty — nothing to show.", chat_id)
        else:
            lines = ["📜 <b>Which product's history?</b>\n"]
            for i, row in enumerate(all_rows[1:], 1):
                name = row[0] if len(row) > 0 else "?"
                lines.append(f"{i}. {name}")
            lines.append("\nReply with:\n• <code>/history 1</code> — show history for #1")
            send_telegram_message("\n".join(lines), chat_id)
        return

    history_idx = parse_history_command(text)
    if history_idx is not None:
        all_rows = products_ws.get_all_values()
        data_count = len(all_rows) - 1
        if history_idx < 1 or history_idx > data_count:
            send_telegram_message(
                f"⚠️ Invalid number. You have {data_count} product(s). "
                f"Use /list to see them.", chat_id,
            )
        elif history_ws:
            product_name = all_rows[history_idx][0]
            hist_rows = history_ws.get_all_values()
            matches = [r for r in hist_rows[1:] if r[1] == product_name]
            if not matches:
                send_telegram_message(
                    f"📜 No history yet for <b>{product_name}</b>.\n"
                    "History is recorded during hourly price checks.",
                    chat_id,
                )
            else:
                recent = matches[-10:]
                lines = [f"📜 <b>Price History: {product_name}</b>\n"]
                for entry in reversed(recent):
                    date = entry[0] if len(entry) > 0 else "?"
                    price = entry[2] if len(entry) > 2 else "?"
                    lines.append(f"  {date} — ₹{price}")
                send_telegram_message("\n".join(lines), chat_id)
        else:
            send_telegram_message("⚠️ Price history is not available.", chat_id)
        return

    # ── /pause — Pause tracking ──
    if text_lower.strip() == "/pause" or text_lower.strip().startswith("/pause@"):
        all_rows = products_ws.get_all_values()
        if len(all_rows) <= 1:
            send_telegram_message("📋 Watchlist is empty.", chat_id)
        else:
            lines = ["⏸️ <b>Which product to pause?</b>\n"]
            for i, row in enumerate(all_rows[1:], 1):
                name = row[0] if len(row) > 0 else "?"
                status = row[5] if len(row) > 5 else "active"
                icon = "⏸️" if status == "paused" else "🟢"
                lines.append(f"{icon} {i}. {name}")
            lines.append("\nReply with:\n• <code>/pause 1</code> — pause #1")
            send_telegram_message("\n".join(lines), chat_id)
        return

    pause_idx = parse_pause_command(text)
    if pause_idx is not None:
        all_rows = products_ws.get_all_values()
        data_count = len(all_rows) - 1
        if pause_idx < 1 or pause_idx > data_count:
            send_telegram_message(
                f"⚠️ Invalid number. You have {data_count} product(s).", chat_id,
            )
        else:
            name = all_rows[pause_idx][0] if len(all_rows[pause_idx]) > 0 else "?"
            products_ws.update_cell(pause_idx + 1, 6, "paused")
            send_telegram_message(
                f"⏸️ Paused tracking for <b>{name}</b>.\n"
                f"Use <code>/resume {pause_idx}</code> to resume.",
                chat_id,
            )
        return

    # ── /resume — Resume tracking ──
    if text_lower.strip() == "/resume" or text_lower.strip().startswith("/resume@"):
        all_rows = products_ws.get_all_values()
        if len(all_rows) <= 1:
            send_telegram_message("📋 Watchlist is empty.", chat_id)
        else:
            lines = ["▶️ <b>Which product to resume?</b>\n"]
            for i, row in enumerate(all_rows[1:], 1):
                name = row[0] if len(row) > 0 else "?"
                status = row[5] if len(row) > 5 else "active"
                icon = "⏸️" if status == "paused" else "🟢"
                lines.append(f"{icon} {i}. {name}")
            lines.append("\nReply with:\n• <code>/resume 1</code> — resume #1")
            send_telegram_message("\n".join(lines), chat_id)
        return

    resume_idx = parse_resume_command(text)
    if resume_idx is not None:
        all_rows = products_ws.get_all_values()
        data_count = len(all_rows) - 1
        if resume_idx < 1 or resume_idx > data_count:
            send_telegram_message(
                f"⚠️ Invalid number. You have {data_count} product(s).", chat_id,
            )
        else:
            name = all_rows[resume_idx][0] if len(all_rows[resume_idx]) > 0 else "?"
            products_ws.update_cell(resume_idx + 1, 6, "active")
            send_telegram_message(
                f"▶️ Resumed tracking for <b>{name}</b>.",
                chat_id,
            )
        return

    # ── Auto-detect URL (no /add needed) ──
    url_detected = detect_url_in_text(text)
    if url_detected:
        url, target_price = url_detected
        log.info("📥 URL detected: %s (target: %s)", url, target_price or "auto")
        msg = handle_add_product(products_ws, url, target_price)
        send_telegram_message(msg, chat_id)
        return


# ──────────────────────────── Flask App ───────────────────────────

app = Flask(__name__)

# Register bot commands at import time (for gunicorn on Render)
# Run in background thread to avoid blocking worker startup
import threading
if TELEGRAM_TOKEN:
    threading.Thread(target=register_bot_commands, daemon=True).start()


@app.route("/health", methods=["GET"])
def health():
    """Health check endpoint."""
    return jsonify({"status": "ok", "service": "price-drop-hunter"})


@app.route("/webhook", methods=["POST"])
def webhook():
    """
    Telegram webhook — processes a single incoming message instantly.
    """
    # Verify secret token (if configured)
    if WEBHOOK_SECRET:
        token = flask_request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
        if token != WEBHOOK_SECRET:
            log.warning("⚠️ Webhook: invalid secret token.")
            return jsonify({"error": "unauthorized"}), 403

    data = flask_request.get_json(silent=True) or {}
    message = data.get("message", {})
    text = message.get("text", "")
    chat_id = str(message.get("chat", {}).get("id", CHAT_ID))

    if not text:
        return jsonify({"ok": True})

    log.info("📨 Webhook message from %s: %s", chat_id, text[:80])

    try:
        sheet = connect_to_sheet()
        products_ws = get_products_worksheet(sheet)
        history_ws = get_history_worksheet(sheet)
        process_single_message(text, chat_id, products_ws, history_ws)
    except Exception as exc:
        log.error("Webhook processing error: %s", exc)
        send_telegram_message("❌ Something went wrong. Please try again.", chat_id)

    return jsonify({"ok": True})


@app.route("/check-prices", methods=["POST"])
def check_prices_endpoint():
    """
    Triggered by GitHub Actions (hourly) to check all prices
    and send notifications.
    """
    # Verify authorization
    if WEBHOOK_SECRET:
        auth = flask_request.headers.get("Authorization", "")
        if auth != f"Bearer {WEBHOOK_SECRET}":
            return jsonify({"error": "unauthorized"}), 403

    log.info("⏰ Price check triggered via /check-prices endpoint.")

    try:
        validate_config()
        sheet = connect_to_sheet()
        products_ws = get_products_worksheet(sheet)
        history_ws = get_history_worksheet(sheet)

        total = max(0, len(products_ws.get_all_values()) - 1)
        alerts, changes = phase2_check_prices(products_ws, history_ws)
        phase3_notify([], alerts, changes, total, products_ws)

        return jsonify({
            "ok": True,
            "products_checked": total,
            "alerts": len(alerts),
            "changes": len(changes),
        })
    except Exception as exc:
        log.error("Price check error: %s", exc)
        return jsonify({"error": str(exc)}), 500


# ──────────────────────────── Main ───────────────────────────────
def main() -> None:
    """Classic standalone mode: process commands + check prices + notify."""
    validate_config()

    # Register bot commands (creates the clickable menu in Telegram)
    register_bot_commands()

    # Connect to Google Sheets
    sheet = connect_to_sheet()
    products_ws = get_products_worksheet(sheet)
    settings_ws = get_settings_worksheet(sheet)
    history_ws = get_history_worksheet(sheet)

    # Phase 1 — Process new Telegram commands
    added_messages = phase1_process_commands(settings_ws, products_ws, history_ws)

    # Phase 2 — Check all tracked prices
    total = max(0, len(products_ws.get_all_values()) - 1)
    alerts, changes = phase2_check_prices(products_ws, history_ws)

    # Phase 3 — Send consolidated notification
    phase3_notify(added_messages, alerts, changes, total, products_ws)

    log.info("🏁 Done.")


def setup_telegram_webhook(render_url: str) -> None:
    """Register the Telegram webhook to point to our Render URL."""
    webhook_url = f"{render_url.rstrip('/')}/webhook"
    api_url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/setWebhook"
    payload = {
        "url": webhook_url,
        "allowed_updates": ["message"],
    }
    if WEBHOOK_SECRET:
        payload["secret_token"] = WEBHOOK_SECRET

    resp = requests.post(api_url, json=payload, timeout=10)
    if resp.ok:
        log.info("✅ Telegram webhook set to: %s", webhook_url)
    else:
        log.error("❌ Failed to set webhook: %s", resp.text)


if __name__ == "__main__":
    if "--serve" in sys.argv:
        # Webhook server mode (for local testing)
        validate_config()
        register_bot_commands()
        # Set up webhook if Render URL provided
        render_url = os.environ.get("RENDER_EXTERNAL_URL", "")
        if render_url:
            setup_telegram_webhook(render_url)
        port = int(os.environ.get("PORT", 5000))
        log.info("🚀 Starting webhook server on port %d", port)
        app.run(host="0.0.0.0", port=port, debug=True)
    elif "--set-webhook" in sys.argv:
        # One-time: set the Telegram webhook URL
        validate_config()
        render_url = sys.argv[sys.argv.index("--set-webhook") + 1]
        setup_telegram_webhook(render_url)
    else:
        # Classic standalone mode (GitHub Actions / local testing)
        main()
