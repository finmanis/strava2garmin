# strava2garmin

Automatically syncs swim workouts from Strava to Garmin Connect, including per-lap split data. Runs on a schedule via GitHub Actions — set it up once and forget it.

This was made as a workaround to get FORM swims synced onto Garmin devices using Strava automatically, as FORM has no direct API access. BLE workarounds exist for that, but since FORM automatically syncs to Strava and Strava has a public API, this is easier. This tool reads swim activities from the Strava API, builds a FIT file with full lap splits, and uploads it to Garmin Connect. It tracks the last synced activity ID so each swim is only uploaded once. This could be expanded to other activities as well, all that would need to be changed is how the .fit file is built and the Strava activity parsing, but the infrastructure for auto-syncing is setup here.

## How it works

1. GitHub Actions runs the sync every 6 hours (and on demand).
2. The script fetches new swim activities from the Strava API since the last run.
3. For each new swim, it fetches lap data, builds a FIT file, and uploads it to Garmin Connect.
4. State (`last_synced_id`, rotating Strava refresh token, Garmin auth tokens) is persisted to a secret GitHub Gist between runs.

## Prerequisites

- A [Strava](https://www.strava.com) account with swims synced from FORM (or any device)
- A [Garmin Connect](https://connect.garmin.com) account
- A GitHub account
- Python 3.10+ (for local setup steps only)

## Setup

### Step 0 - Create a new repo using this template.

This is a template repo. In the top right, click "Use this template" and "Create a new repository" to make your own repo from this. I'd recommend making this private in case you accidentally leak any secrets.

### Step 1 — Create your Strava API app

Go to [strava.com/settings/api](https://www.strava.com/settings/api) and create an application:
- **Website:** `http://localhost`
- **Authorization Callback Domain:** `localhost`

Note your **Client ID** and **Client Secret** — the setup wizard will ask for them.

> **Strava subscription required (as of June 1, 2026).** Strava's updated Developer Program requires a Strava subscription to access the API as a new Standard Tier developer. After creating your app, open your [API settings dashboard](https://www.strava.com/settings/api) and self-upgrade to **Standard Tier** access (covers up to 10 athletes — more than enough for personal use). Existing developers from before the change get a transition plan from Strava.

---

### Step 2 — Clone the repo and run the setup wizard

```bash
git clone https://github.com/YOUR_USERNAME/YOUR_REPO_NAME
cd YOUR_REPO_NAME
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Install the [GitHub CLI](https://cli.github.com) if you don't have it, then authenticate:

```bash
gh auth login --scopes gist
```

Then run the wizard:

```bash
python setup.py
```

The wizard will:
1. Open a browser for the Strava OAuth flow and capture the refresh token
2. Prompt for your Garmin email, password, and MFA code to cache tokens locally
3. Create a secret GitHub Gist with all three state files
4. Set all 7 GitHub Actions secrets automatically
5. Write your `.env` for local use

---

### Step 3 — Run the workflow

Go to your repo → **Actions → Sync FORM swims to Garmin → Run workflow**.

The first run will upload all your historical swims. After that, the workflow runs automatically every 6 hours.

To test without uploading anything:

```bash
source .venv/bin/activate
python sync.py --dry-run
```

---

## Running manually

You can run the sync script locally at any time without GitHub Actions. All secrets are loaded from your `.env` file.

### Security warning

> **Keep your `.env` file private.** It contains credentials for your Strava account, Garmin Connect account, and GitHub — anyone with access to it can read your activities, upload to your Garmin account, and access your Gist data. Never commit `.env` to git, share it, or store it in cloud sync folders (Dropbox, iCloud, etc.). The `.gitignore` already excludes it, but double-check with `git status` before pushing.

### Required `.env` values

Make sure your `.env` file contains all of the following (copy `.env.example` if you haven't already):

```
STRAVA_CLIENT_ID=your_client_id
STRAVA_CLIENT_SECRET=your_client_secret
STRAVA_REFRESH_TOKEN=your_refresh_token
GARMIN_EMAIL=your_garmin_email@example.com
GARMIN_PASSWORD=your_garmin_password
GIST_ID=your_gist_id
GH_PAT=your_github_personal_access_token
```

### Commands

```bash
source .venv/bin/activate

# Full sync — fetches new swims from Strava and uploads to Garmin
python sync.py

# Dry run — fetches from Strava but skips the Garmin upload (safe to test)
python sync.py --dry-run
```

State files (`last_synced_id.txt`, `strava_refresh_token.txt`, `garmin_tokens.json`) are read from and written to the local directory when running manually. The Gist is also updated at the end of a successful run — this keeps CI and local state in sync if you alternate between the two.

---

## Maintenance

**Garmin token expiry:** The cached Garmin tokens are long-lived but may eventually expire. If the workflow starts failing with authentication errors, run `get_garmin_token.py` locally again and update the `garmin_tokens.json` file in your Gist.

**Strava token rotation:** Strava refresh tokens rotate on every use. The workflow handles this automatically — the new token is written back to the Gist after each run.

**Re-syncing from scratch:** Edit your Gist and set `last_synced_id.txt` to `0`. The next run will re-upload all swims. Delete any existing duplicates in Garmin Connect first. Deleting in bulk within Garmin Connect is really tedious and annoying, so I would recommend taking note of your last synced ID to resume from the same spot.

**Strava API base URL migration:** Strava is moving its data API base from `https://www.strava.com/api/v3` to `https://www.api-v3.strava.com` (announced for June 2026). `sync.py` defaults to the legacy base and reads an optional `STRAVA_API_BASE` override, so no action is needed until the new host is live. To check whether it's live:

```bash
dig +short www.api-v3.strava.com
```

Once that returns an IP address, switch over:

- **GitHub Actions:** uncomment the `STRAVA_API_BASE` line in the *Run sync* step of [.github/workflows/sync.yml](.github/workflows/sync.yml).
- **Local runs:** add `STRAVA_API_BASE=https://www.api-v3.strava.com` to your `.env`.

The OAuth endpoints (token refresh, authorization) stay on `www.strava.com` and are unaffected.

---

## Disclaimer

The Strava integration uses the official, documented Strava API — fully within their Terms of Service.

The Garmin integration uses an unofficial, undocumented API (the same one the Garmin Connect website uses internally). Garmin does not provide a free public API for activity uploads. Using this tool moves your own data between your own accounts, but it may technically fall outside Garmin's Terms of Service. Use at your own discretion. In practice, this approach is widely used by the open-source community and Garmin has historically tolerated it.
