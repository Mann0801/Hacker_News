#!/usr/bin/env python3
"""Daily Hacker News digest: the 10 most-discussed stories, summarized by Gemini, emailed to you."""

import argparse
import html
import logging
import os
import re
import smtplib
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from email.message import EmailMessage
from urllib.parse import urlparse

import requests
import trafilatura
from dotenv import load_dotenv
from google import genai
from google.genai import errors, types

HN_API = "https://hacker-news.firebaseio.com/v0"
HN_ITEM_URL = "https://news.ycombinator.com/item?id={id}"

CANDIDATE_COUNT = 40
DIGEST_SIZE = 5
MIN_COMMENTS = 5

HN_TIMEOUT = 10           # seconds per HN API call
ARTICLE_TIMEOUT = 7       # seconds per article fetch
MAX_ARTICLE_CHARS = 40_000  # bound per-story token cost; plenty for a one-paragraph summary
MAX_WORKERS = 10

# Aliases for Google's current Flash-Lite / Flash models; override the first with GEMINI_MODEL.
# Flash-Lite writes summaries as good as Flash's and is less often overloaded; Flash has its own
# free-tier quota, so it's the backup when Flash-Lite is rate-limited or down.
DEFAULT_MODEL = "gemini-flash-lite-latest"
BACKUP_MODEL = "gemini-flash-latest"

# The free tier allows ~5 requests/minute per model, so send one request every 13s per model.
GEMINI_MIN_INTERVAL = 13
GEMINI_ATTEMPTS = 3         # per model
GEMINI_MAX_WAIT = 65        # a longer suggested wait means the daily quota is used up — don't wait

SUMMARY_SYSTEM_PROMPT = (
    "You write entries for a daily news email about stories trending on Hacker News. The reader "
    "is curious and smart but not an expert in every field, so explain each story the way you'd "
    "tell a friend over coffee.\n\n"
    "Write ONE paragraph of 4-6 short sentences in plain, everyday English:\n"
    "- Start with what actually happened, in one simple sentence.\n"
    "- Give the background needed to understand it: who is involved, what the thing is, and "
    "what led up to it.\n"
    "- End with why it matters, phrased to fit the story (don't open with a stock phrase like "
    "\"People are talking about this because\" — these entries are read together).\n\n"
    "Rules: use simple words and short sentences. If you must use a technical term or acronym, "
    "explain it in a few words (e.g. \"F-Droid, an app store for free and open-source Android "
    "apps\"). Don't copy phrases from the article — put it in your own words. Keep only the "
    "facts that help understanding; skip minor details. No headings, bullet points, or "
    "markdown — just the paragraph. If you only have the title, explain what it likely refers "
    "to without inventing details, and keep it short."
)

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/128.0 Safari/537.36 hn-digest/1.0"
)

log = logging.getLogger("hn-digest")
session = requests.Session()
session.headers["User-Agent"] = USER_AGENT


# ---------------------------------------------------------------------------
# Hacker News
# ---------------------------------------------------------------------------

def fetch_top_story_ids(limit: int = CANDIDATE_COUNT) -> list[int]:
    resp = session.get(f"{HN_API}/topstories.json", timeout=HN_TIMEOUT)
    resp.raise_for_status()
    return resp.json()[:limit]


def fetch_story_details(story_id: int) -> dict | None:
    """Return the HN item dict, or None if it couldn't be fetched."""
    try:
        resp = session.get(f"{HN_API}/item/{story_id}.json", timeout=HN_TIMEOUT)
        resp.raise_for_status()
        return resp.json()
    except (requests.RequestException, ValueError) as e:
        log.warning("Could not fetch HN item %s: %s", story_id, e)
        return None


def interest_score(story: dict) -> int:
    return story.get("score", 0) + story.get("descendants", 0) * 2


def select_top_stories(stories: list[dict]) -> list[dict]:
    eligible = [
        s for s in stories
        if s
        and s.get("type") == "story"
        and s.get("url")
        and not s.get("dead")
        and not s.get("deleted")
        and s.get("descendants", 0) >= MIN_COMMENTS
    ]
    eligible.sort(key=interest_score, reverse=True)
    return eligible[:DIGEST_SIZE]


# ---------------------------------------------------------------------------
# Article fetching + summarization
# ---------------------------------------------------------------------------

def fetch_article(url: str) -> tuple[str | None, str | None]:
    """Download the article; return (main text, meta description). Either may be None."""
    try:
        resp = session.get(url, timeout=ARTICLE_TIMEOUT)
        resp.raise_for_status()
        content_type = resp.headers.get("Content-Type", "")
        if "html" not in content_type and "text" not in content_type:
            log.info("Skipping non-HTML content (%s) at %s", content_type, url)
            return None, None
        page = resp.text
        text = trafilatura.extract(page, include_comments=False, include_tables=False)
        meta = trafilatura.extract_metadata(page)
    except Exception as e:  # network errors, bad encodings, parser errors — all non-fatal
        log.info("Article fetch failed for %s: %s", url, e)
        return None, None
    description = (meta.description or "").strip() if meta else ""
    if not text or len(text.strip()) < 200:
        text = None
    return (text.strip()[:MAX_ARTICLE_CHARS] if text else None), (description or None)


def make_gemini_client() -> genai.Client:
    return genai.Client(
        api_key=os.environ["GEMINI_API_KEY"],
        http_options=types.HttpOptions(timeout=60_000),  # milliseconds
    )


_last_request: dict[str, float] = {}


def _pace(model: str) -> None:
    """Sleep so requests to one model are at least GEMINI_MIN_INTERVAL seconds apart."""
    wait = _last_request.get(model, 0) + GEMINI_MIN_INTERVAL - time.monotonic()
    if wait > 0:
        time.sleep(wait)
    _last_request[model] = time.monotonic()


def _describe(e: Exception) -> str:
    if isinstance(e, errors.APIError):
        return f"{e.code} {e.status}: {(e.message or '').splitlines()[0]}"
    return str(e)


def call_gemini(client: genai.Client, model: str, prompt: str) -> str:
    """One summary from one model, retrying rate limits and overloads. Raises on failure."""
    for attempt in range(1, GEMINI_ATTEMPTS + 1):
        _pace(model)
        try:
            response = client.models.generate_content(
                model=model,
                contents=prompt,
                config=types.GenerateContentConfig(
                    system_instruction=SUMMARY_SYSTEM_PROMPT,
                    automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
                ),
            )
            summary = (response.text or "").strip()
            if not summary:
                raise RuntimeError("empty response")
            return summary
        except errors.APIError as e:
            if e.code == 429:
                # Google says how long to wait, e.g. "Please retry in 9.55s."
                m = re.search(r"retry in ([\d.]+)s", str(e))
                delay = float(m.group(1)) + 1 if m else 30
                if delay > GEMINI_MAX_WAIT:
                    raise
            elif e.code in (500, 503, 504):
                delay = 10 * attempt
            else:
                raise
            if attempt == GEMINI_ATTEMPTS:
                raise
            log.info("%s on %s, retrying in %.0fs", _describe(e), model, delay)
            time.sleep(delay)
    raise AssertionError("unreachable")


def build_prompt(story: dict) -> str:
    parts = [f"Title: {story['title']}", f"URL: {story['url']}"]
    if story.get("text"):
        parts.append(f"Submitter's text: {story['text']}")
    if story["article"]:
        parts.append(f"<article>\n{story['article']}\n</article>")
    else:
        if story["description"]:
            parts.append(f"Page description: {story['description']}")
        parts.append("(The article text could not be fetched — summarize from what is above.)")
    return "\n\n".join(parts)


# Models that already failed a story this run. They're skipped for the rest of the run: when
# Flash is overloaded or out of quota, retrying it on every story just wastes minutes.
_given_up: set[str] = set()


def summarize_story(story: dict, client: genai.Client, model: str) -> str:
    """Return a one-paragraph summary.

    Never raises: tries `model`, then BACKUP_MODEL; if both fail, falls back to the article's
    meta description, or to an empty summary (the email then shows just the title and links).
    """
    prompt = build_prompt(story)
    for m in dict.fromkeys([model, BACKUP_MODEL]):  # dedupe if GEMINI_MODEL is the backup
        if m in _given_up:
            continue
        try:
            summary = call_gemini(client, m, prompt)
            story["summary_source"] = "primary" if m == model else "backup"
            return summary
        except Exception as e:
            log.warning("%s failed for story %s (%s): %s", m, story["id"], story["title"], _describe(e))
            if isinstance(e, errors.APIError) and e.code in (429, 500, 503, 504):
                log.warning("Skipping %s for the rest of this run", m)
                _given_up.add(m)

    if story["description"]:
        story["summary_source"] = "meta"
        return story["description"]
    story["summary_source"] = "none"
    return ""


def summarize_all(stories: list[dict], client: genai.Client, model: str) -> list[dict]:
    """Fetch articles in parallel, then summarize one at a time (free-tier rate limits).

    Modifies stories in place, preserves rank order, and never drops a story.
    """
    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        fetched = list(pool.map(lambda s: fetch_article(s["url"]), stories))
    for story, (article, description) in zip(stories, fetched):
        story["article"], story["description"] = article, description
        story["article_fetched"] = article is not None

    for i, story in enumerate(stories, 1):
        story["summary"] = summarize_story(story, client, model)
        log.info("%d/%d [%s] %s", i, len(stories), story["summary_source"], story["title"])
    return stories


# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------

def _domain(url: str) -> str:
    host = urlparse(url).netloc
    return host.removeprefix("www.")


def build_email_html(stories: list[dict]) -> str:
    date_str = datetime.now().strftime("%A, %B %-d, %Y")
    items = []
    for rank, s in enumerate(stories, 1):
        hn_link = HN_ITEM_URL.format(id=s["id"])
        summary_html = (
            f'<p style="margin:8px 0 8px 0;font:15px/1.55 -apple-system,Segoe UI,Helvetica,Arial,sans-serif;color:#333;">{html.escape(s["summary"])}</p>'
            if s["summary"] else '<div style="height:8px"></div>'
        )
        items.append(f"""
      <tr><td style="padding:0 0 28px 0;">
        <table role="presentation" cellpadding="0" cellspacing="0" width="100%"><tr>
          <td valign="top" style="width:36px;font:600 20px/1.3 Georgia,serif;color:#ff6600;">{rank}.</td>
          <td valign="top">
            <a href="{html.escape(s['url'])}" style="font:600 17px/1.35 -apple-system,Segoe UI,Helvetica,Arial,sans-serif;color:#1a1a1a;text-decoration:none;">{html.escape(s['title'])}</a>
            <span style="font:13px -apple-system,Segoe UI,Helvetica,Arial,sans-serif;color:#888;"> ({html.escape(_domain(s['url']))})</span>
            {summary_html}
            <div style="font:13px -apple-system,Segoe UI,Helvetica,Arial,sans-serif;color:#888;">
              {s.get('score', 0)} points &middot; {s.get('descendants', 0)} comments &middot;
              <a href="{hn_link}" style="color:#ff6600;">HN discussion</a>
            </div>
          </td>
        </tr></table>
      </td></tr>""")

    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#f6f6ef;">
  <table role="presentation" cellpadding="0" cellspacing="0" width="100%" style="background:#f6f6ef;"><tr><td align="center" style="padding:24px 12px;">
    <table role="presentation" cellpadding="0" cellspacing="0" width="100%" style="max-width:640px;background:#ffffff;border-radius:8px;">
      <tr><td style="padding:24px 28px;border-bottom:3px solid #ff6600;">
        <div style="font:700 22px -apple-system,Segoe UI,Helvetica,Arial,sans-serif;color:#1a1a1a;">Hacker News Daily Digest</div>
        <div style="font:14px -apple-system,Segoe UI,Helvetica,Arial,sans-serif;color:#888;margin-top:4px;">{date_str} &middot; the {len(stories)} most-discussed stories</div>
      </td></tr>
      <tr><td style="padding:28px 28px 4px 28px;">
        <table role="presentation" cellpadding="0" cellspacing="0" width="100%">{''.join(items)}
        </table>
      </td></tr>
    </table>
  </td></tr></table>
</body></html>"""


def build_email_text(stories: list[dict]) -> str:
    lines = [f"Hacker News Daily Digest — {datetime.now():%A, %B %-d, %Y}", ""]
    for rank, s in enumerate(stories, 1):
        lines += [
            f"{rank}. {s['title']}",
            f"   {s['url']}",
            "",
            *([f"   {s['summary']}", ""] if s["summary"] else []),
            f"   {s.get('score', 0)} points · {s.get('descendants', 0)} comments · "
            f"{HN_ITEM_URL.format(id=s['id'])}",
            "",
            "",
        ]
    return "\n".join(lines)


def send_email(html_body: str, text_body: str) -> None:
    sender = os.environ["EMAIL_ADDRESS"]
    password = os.environ["EMAIL_APP_PASSWORD"].replace(" ", "")  # Gmail shows it with spaces
    recipients = [r.strip() for r in os.environ["EMAIL_TO"].split(",") if r.strip()]

    msg = EmailMessage()
    msg["Subject"] = f"HN Digest — {datetime.now():%b %-d, %Y}"
    msg["From"] = sender
    msg["To"] = ", ".join(recipients)
    msg.set_content(text_body)
    msg.add_alternative(html_body, subtype="html")

    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as smtp:
        smtp.login(sender, password)
        smtp.send_message(msg)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

REQUIRED_ENV = ["GEMINI_API_KEY", "EMAIL_ADDRESS", "EMAIL_APP_PASSWORD", "EMAIL_TO"]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Don't send email; write digest_preview.html / .txt to the current directory instead.",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    for noisy in ("httpx", "google_genai"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

    required = REQUIRED_ENV[:1] if args.dry_run else REQUIRED_ENV
    missing = [k for k in required if not os.environ.get(k)]
    if missing:
        log.error("Missing environment variables: %s (see .env.example)", ", ".join(missing))
        return 2

    try:
        ids = fetch_top_story_ids()
    except (requests.RequestException, ValueError) as e:
        log.error("Could not fetch HN top stories: %s", e)
        return 1
    log.info("Fetched %d top story IDs", len(ids))

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        stories = list(pool.map(fetch_story_details, ids))

    top = select_top_stories(stories)
    log.info("Selected %d stories after filtering and ranking", len(top))
    if not top:
        log.error("No eligible stories found; not sending a digest")
        return 1

    model = os.environ.get("GEMINI_MODEL") or DEFAULT_MODEL
    summarized = summarize_all(top, make_gemini_client(), model)
    by_source = {
        k: sum(s["summary_source"] == k for s in summarized)
        for k in ("primary", "backup", "meta", "none")
    }
    log.info(
        "Summaries: %d %s, %d %s (backup), %d meta-description fallback, %d title-only",
        by_source["primary"], model, by_source["backup"], BACKUP_MODEL,
        by_source["meta"], by_source["none"],
    )
    if by_source["primary"] + by_source["backup"] == 0:
        log.warning("No Gemini summaries at all — check GEMINI_API_KEY / GEMINI_MODEL")

    html_body = build_email_html(summarized)
    text_body = build_email_text(summarized)

    if args.dry_run:
        with open("digest_preview.html", "w", encoding="utf-8") as f:
            f.write(html_body)
        with open("digest_preview.txt", "w", encoding="utf-8") as f:
            f.write(text_body)
        log.info("Dry run: wrote digest_preview.html and digest_preview.txt")
        return 0

    try:
        send_email(html_body, text_body)
    except smtplib.SMTPAuthenticationError as e:
        log.error("Gmail rejected the login — check EMAIL_ADDRESS / EMAIL_APP_PASSWORD: %s", e)
        return 1
    except (smtplib.SMTPException, OSError) as e:
        log.error("Failed to send email: %s", e)
        return 1
    log.info("Digest sent to %s", os.environ["EMAIL_TO"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
