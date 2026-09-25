# Hacker News Daily Digest

Emails you the 5 most-discussed Hacker News stories every morning. Each one comes with a short, plain-English explanation written by Google Gemini. Everything it uses is free.

<p align="center">
  <img src="docs/email.png" alt="Example digest email" width="560">
</p>

## How it works

Every morning, GitHub Actions starts a fresh computer and runs `digest.py`. The script gets the top 40 stories from the Hacker News API and drops job ads, posts with no article link, and stories with fewer than 5 comments. It ranks the rest by points + (comments × 2). Comments count double because the goal is the most *discussed* stories, not just the most liked. The top 5 make the email.

For each story, the script downloads the article and keeps only the main text, without menus or ads. It sends that text to Gemini and asks for a simple 4–6 sentence explanation: what happened, the background, and why it matters. The free tier allows only about 5 requests per minute, so requests go out one at a time, about 13 seconds apart. If Gemini is busy, the script waits and retries, then tries a backup Gemini model. If both fail, it uses the article's own short description, so a story is never dropped.

Last, it builds the email: numbered stories with clickable titles, the explanations, points and comment counts, and a link to each HN discussion. It sends the email through your Gmail account using an App Password. Your keys live in `.env` on your computer and in encrypted GitHub Secrets, never in the code.

## Setup

**1. Install**

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
```

**2. Get your keys and put them in `.env`**

- `GEMINI_API_KEY`: a free key from <https://aistudio.google.com/apikey>.
- `EMAIL_ADDRESS`: the Gmail account that sends the digest.
- `EMAIL_APP_PASSWORD`: turn on 2-Step Verification, then create an App Password at <https://myaccount.google.com/apppasswords>. Don't use your normal Gmail password.
- `EMAIL_TO`: where the digest goes.

## Use

```bash
.venv/bin/python digest.py --dry-run   # preview only: writes digest_preview.html
.venv/bin/python digest.py             # send the email
```

## Run it every day (GitHub Actions)

1. Push this repo to GitHub.
2. Go to **Settings → Secrets and variables → Actions** and add the same 4 values from `.env` as secrets.
3. It then emails you every day at 8:00 AM IST. To send one right away, go to **Actions → Daily HN digest → Run workflow**.

To change the time, edit the `cron` line in `.github/workflows/digest.yml`. The time is in UTC.
