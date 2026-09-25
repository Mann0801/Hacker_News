# Hacker News Daily Digest

Emails you the 5 most-discussed Hacker News stories each day, each with a one-paragraph summary from Google Gemini.

**It costs $0 to run.** Every service it uses is free:

| Service | Used for | Cost |
|---|---|---|
| Hacker News API | Story data | Free, no key needed |
| Gemini API free tier | Summaries | Free, no credit card needed |
| Gmail SMTP | Sending the email | Free |
| GitHub Actions (or local cron) | Daily scheduling | Free |

## How it works

1. Pulls the top 40 story IDs from the official HN API and fetches their details in parallel.
2. Drops job posts, self-posts with no link, dead or deleted items, and stories with fewer than 5 comments.
3. Ranks what's left by `interest_score = score + 2 × comments` and keeps the top 5. Change `DIGEST_SIZE` in `digest.py` to get more or fewer.
4. It downloads all 5 articles in parallel (7-second timeout each) and extracts the main text with `trafilatura`. Then it asks Gemini for a 3–5 sentence summary of each story, one request at a time and about 13 seconds apart. The free tier allows about 5 requests per minute per model, so the summary step takes about a minute. If an article's text can't be extracted, Gemini summarizes from the title, any submitter text, and the page's meta description.
5. It sends an HTML email with a plain-text fallback through Gmail SMTP.

**If Gemini fails for a story:**

1. If Gemini is rate-limited, the script waits as long as Google asks. If it's overloaded, the script retries with backoff.
2. If the main model (`gemini-flash-lite-latest`) still fails, the script tries `gemini-flash-latest`, which has its own free quota. After a model fails once, it's skipped for the rest of that run, so one overloaded model doesn't slow down every story.
3. If both fail, the story uses the article's meta description.
4. If there's no description, the story appears with just its title and links.

Stories are never dropped. The log tags each summary with its source: `[primary]`, `[backup]`, `[meta]`, or `[none]`.

## Setup

Requires Python 3.10+.

```bash
cd ~/Documents/Hackernews
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
cp .env.example .env
chmod 600 .env        # it holds secrets
```

Then fill in `.env`. The next two sections explain where to get each value.

### Getting a Gmail App Password

Gmail doesn't accept your normal password over SMTP. It needs an App Password, and those require 2-Step Verification.

1. Turn on 2-Step Verification at <https://myaccount.google.com/security> if it isn't on already.
2. Go to <https://myaccount.google.com/apppasswords>. You can also search "App passwords" in your Google Account settings.
3. Enter a name such as `hn-digest` and click **Create**.
4. Copy the 16-character password into `EMAIL_APP_PASSWORD`. It's fine to keep the spaces.

If the App passwords page says the setting isn't available, 2-Step Verification is off or your Workspace admin has disabled App Passwords.

### Getting a free Gemini API key

1. Go to <https://aistudio.google.com/apikey> and sign in with a Google account.
2. Click **Create API key**. Accept the terms if asked, and pick or create a project when prompted.
3. Copy the key into `GEMINI_API_KEY` in `.env`.

No credit card is needed. Don't enable billing on that Google Cloud project, because that moves the key off the free tier. This project makes about 5 requests a day, far below the free tier's daily limit.

Note: on the free tier, Google may use your prompts to improve its products. Here those prompts are just public news articles.

The default model is `gemini-flash-lite-latest`, Google's alias for its current Flash-Lite model. For short summaries its quality matches Flash, and it's less often overloaded. To use a different model, set `GEMINI_MODEL` in `.env`.

## Running it manually

```bash
# Preview: builds the digest and writes digest_preview.html / .txt, sends nothing
.venv/bin/python digest.py --dry-run
xdg-open digest_preview.html

# Real run: builds and emails the digest
.venv/bin/python digest.py
```

A run usually takes about a minute, mostly waiting between Gemini requests. Progress is logged to stderr.

Exit codes: `0` means success, `1` means the run failed (network, auth, or no stories), and `2` means environment variables are missing.

## Scheduling daily (GitHub Actions)

The script runs in the cloud on GitHub's servers, so your computer can be off. See `.github/workflows/digest.yml`.

1. Create a **private** GitHub repo and push this folder to it. `.env` is gitignored and won't be uploaded.
2. In the repo, go to **Settings → Secrets and variables → Actions → New repository secret**. Add these four secrets with the same values as your `.env`: `GEMINI_API_KEY`, `EMAIL_ADDRESS`, `EMAIL_APP_PASSWORD`, `EMAIL_TO`.
3. Test it: open the **Actions** tab, choose **Daily HN digest**, and click **Run workflow**. Check your inbox.

After that, it runs every day at 02:30 UTC, which is 8:00 AM IST. To change the time, edit the `cron` line in the workflow. The time is always in UTC. GitHub may start scheduled runs 5–30 minutes late, and it pauses schedules in repos with no activity for 60 days. GitHub emails you before that happens, and one click re-enables it.
