# Hacker News Daily Digest

Emails you the 5 most-discussed Hacker News stories every morning. Each one comes with a short, plain-English explanation written by Google Gemini. Everything it uses is free.

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
