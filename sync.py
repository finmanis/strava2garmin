import argparse
import logging
import os
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone

import requests
from dotenv import load_dotenv
from fit_tool.fit_file_builder import FitFileBuilder
from fit_tool.profile.messages.activity_message import ActivityMessage
from fit_tool.profile.messages.file_id_message import FileIdMessage
from fit_tool.profile.messages.lap_message import LapMessage
from fit_tool.profile.messages.session_message import SessionMessage
from fit_tool.profile.profile_type import (
    Event,
    EventType,
    FileType,
    LapTrigger,
    SessionTrigger,
    Sport,
    SubSport,
)
from garminconnect import Garmin

load_dotenv()

logging.basicConfig(
    level=logging.DEBUG if os.getenv("LOG_LEVEL", "").upper() == "DEBUG" else logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
    force=True,
)
logger = logging.getLogger(__name__)

# OAuth endpoints stay on www.strava.com; only the data API base is moving (Strava API update, June 2026).
# The new host (https://www.api-v3.strava.com) was announced for June 1, 2026 but is not yet
# resolvable in DNS, so we default to the legacy base and allow an env override to switch over
# once the new host is live: STRAVA_API_BASE=https://www.api-v3.strava.com
STRAVA_TOKEN_URL = "https://www.strava.com/oauth/token"
STRAVA_API_BASE = os.getenv("STRAVA_API_BASE", "https://www.strava.com/api/v3")
STRAVA_SWIM_TYPES = {"Swim", "OpenWaterSwim"}
GARMIN_TOKEN_FILE = "./garmin_tokens.json"

# Strava's CloudFront WAF blocks bare bot User-Agents from datacenter IPs (e.g. GitHub Actions
# runners). Sending a browser-like User-Agent + Accept header avoids the 403 "Request blocked".
STRAVA_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json",
}

def _to_fit_ms(dt: datetime) -> int:
    return int(dt.timestamp() * 1000)


def _parse_strava_dt(ts: str) -> datetime:
    """Parse a Strava UTC timestamp string to an aware datetime."""
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def read_last_synced_id(path: str = "last_synced_id.txt") -> int:
    try:
        with open(path, "r") as f:
            return int(f.read().strip())
    except FileNotFoundError:
        logger.info("No last_synced_id.txt found. Starting from the beginning.")
        return 0


def write_last_synced_id(activity_id: int, path: str = "last_synced_id.txt") -> None:
    with open(path, "w") as f:
        f.write(str(activity_id))
    logger.debug("Updated last synced ID to %d.", activity_id)


def write_strava_refresh_token(token: str, path: str = "strava_refresh_token.txt") -> None:
    with open(path, "w") as f:
        f.write(token)
    logger.debug("Saved rotated Strava refresh token.")


def refresh_strava_token(client_id: str, client_secret: str, refresh_token: str) -> tuple[str, str]:
    """Exchange a Strava refresh token for a new access token. Returns (access_token, new_refresh_token)."""
    try:
        response = requests.post(
            STRAVA_TOKEN_URL,
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
            },
            headers=STRAVA_HEADERS,
            timeout=30,
        )
        if response.status_code == 401:
            logger.error(
                "Strava returned 401 during token refresh. "
                "Check STRAVA_CLIENT_ID, STRAVA_CLIENT_SECRET, and refresh token validity."
            )
            response.raise_for_status()
        response.raise_for_status()
    except requests.exceptions.Timeout:
        logger.error("Strava token refresh timed out after 30s. Aborting sync.")
        raise
    except requests.exceptions.HTTPError as e:
        logger.error("HTTP error during Strava token refresh: %s — body: %s", e, response.text)
        raise

    data = response.json()
    access_token: str = data["access_token"]
    new_refresh_token: str = data["refresh_token"]
    logger.info("Strava token refreshed. Expires at %d.", data["expires_at"])
    return access_token, new_refresh_token


def fetch_strava_swims(access_token: str, since_id: int) -> list[dict]:
    """Fetch all swim activities with ID > since_id, sorted ascending by ID."""
    headers = {**STRAVA_HEADERS, "Authorization": f"Bearer {access_token}"}
    swims: list[dict] = []
    page = 1

    while True:
        try:
            response = requests.get(
                f"{STRAVA_API_BASE}/athlete/activities",
                headers=headers,
                params={"per_page": 100, "page": page},
                timeout=30,
            )
            if response.status_code == 401:
                logger.error(
                    "Strava returned 401 fetching activities. "
                    "Access token may be invalid — check token refresh."
                )
                response.raise_for_status()
            response.raise_for_status()
        except requests.exceptions.Timeout:
            logger.error("Strava activities request timed out after 30s on page %d.", page)
            raise
        except requests.exceptions.HTTPError as e:
            logger.error("HTTP error fetching Strava activities (page %d): %s", page, e)
            raise

        activities: list[dict] = response.json()

        if not activities:
            break

        for activity in activities:
            if activity.get("sport_type") in STRAVA_SWIM_TYPES and activity["id"] > since_id:
                swims.append(activity)

        # Strava returns newest first; stop paging once all IDs on this page are ≤ since_id
        if len(activities) < 100 or all(a["id"] <= since_id for a in activities):
            break

        page += 1

    swims.sort(key=lambda a: a["id"])
    logger.info("Found %d new swim(s) since activity ID %d.", len(swims), since_id)
    return swims


def fetch_strava_laps(activity_id: int, access_token: str) -> list[dict]:
    """Fetch lap data for a single Strava activity."""
    url = f"{STRAVA_API_BASE}/activities/{activity_id}/laps"
    headers = {**STRAVA_HEADERS, "Authorization": f"Bearer {access_token}"}
    try:
        response = requests.get(url, headers=headers, timeout=30)
        if response.status_code == 401:
            logger.error(
                "Strava returned 401 fetching laps for activity %d. "
                "Access token may be invalid.",
                activity_id,
            )
            response.raise_for_status()
        response.raise_for_status()
    except requests.exceptions.Timeout:
        logger.error("Strava laps request for activity %d timed out after 30s.", activity_id)
        raise
    except requests.exceptions.HTTPError as e:
        logger.error("HTTP error fetching laps for activity %d: %s", activity_id, e)
        raise

    laps: list[dict] = response.json()
    logger.debug("Fetched %d lap(s) for activity %d.", len(laps), activity_id)
    return laps


def build_swim_fit(activity: dict, laps: list[dict]) -> bytes:
    """Build a FIT file binary from a Strava swim activity and its laps."""
    sport_type = activity.get("sport_type", "Swim")
    sub_sport = SubSport.LAP_SWIMMING if sport_type == "Swim" else SubSport.OPEN_WATER

    start_dt = _parse_strava_dt(activity["start_date"])
    total_elapsed_s: float = activity.get("elapsed_time", 0)
    total_moving_s: float = activity.get("moving_time", 0)
    total_distance_m: float = activity.get("distance", 0)
    avg_hr = activity.get("average_heartrate")
    max_hr = activity.get("max_heartrate")
    calories = activity.get("calories")
    pool_length = activity.get("pool_length")  # meters, may be None

    end_dt = start_dt + timedelta(seconds=total_elapsed_s)

    builder = FitFileBuilder(auto_define=True)

    # FileId
    fid = FileIdMessage()
    fid.type = FileType.ACTIVITY
    fid.time_created = _to_fit_ms(start_dt)
    builder.add(fid)

    # Activity
    act = ActivityMessage()
    act.timestamp = _to_fit_ms(end_dt)
    act.total_timer_time = total_moving_s
    act.num_sessions = 1
    act.type = 0
    act.event = Event.ACTIVITY
    act.event_type = EventType.STOP
    builder.add(act)

    # Session
    sess = SessionMessage()
    sess.start_time = _to_fit_ms(start_dt)
    sess.timestamp = _to_fit_ms(end_dt)
    sess.sport = Sport.SWIMMING
    sess.sub_sport = sub_sport
    sess.total_elapsed_time = total_elapsed_s
    sess.total_timer_time = total_moving_s
    sess.total_distance = total_distance_m
    sess.num_laps = len(laps)
    sess.event = Event.LAP
    sess.event_type = EventType.STOP
    sess.trigger = SessionTrigger.ACTIVITY_END
    if avg_hr is not None:
        sess.avg_heart_rate = int(avg_hr)
    if max_hr is not None:
        sess.max_heart_rate = int(max_hr)
    if calories is not None:
        sess.total_calories = int(calories)
    if pool_length is not None:
        sess.pool_length = pool_length
    builder.add(sess)

    # Laps
    for i, lap in enumerate(laps):
        lap_start_dt = _parse_strava_dt(lap["start_date"])
        lap_elapsed_s: float = lap.get("elapsed_time", 0)
        lap_moving_s: float = lap.get("moving_time", 0)
        lap_distance_m: float = lap.get("distance", 0)

        lap_end_dt = lap_start_dt + timedelta(seconds=lap_elapsed_s)

        lm = LapMessage()
        lm.message_index = i
        lm.start_time = _to_fit_ms(lap_start_dt)
        lm.timestamp = _to_fit_ms(lap_end_dt)
        lm.total_elapsed_time = lap_elapsed_s
        lm.total_timer_time = lap_moving_s
        lm.total_distance = lap_distance_m
        lm.event = Event.LAP
        lm.event_type = EventType.STOP
        lm.trigger = LapTrigger.MANUAL

        avg_speed = lap.get("average_speed")
        if avg_speed is not None:
            lm.avg_speed = avg_speed

        max_speed = lap.get("max_speed")
        if max_speed is not None:
            lm.max_speed = max_speed

        lap_avg_hr = lap.get("average_heartrate")
        if lap_avg_hr is not None:
            lm.avg_heart_rate = int(lap_avg_hr)

        lap_max_hr = lap.get("max_heartrate")
        if lap_max_hr is not None:
            lm.max_heart_rate = int(lap_max_hr)

        # Strava reports cadence as strokes/min for swimming
        cadence = lap.get("average_cadence")
        if cadence is not None:
            lm.avg_cadence = int(cadence)

        builder.add(lm)

    fit_file = builder.build()
    return fit_file.to_bytes()


def _lookup_garmin_activity(garmin_client: Garmin, activity: dict) -> int | None:
    """Return the Garmin activity ID for a Strava activity, or None if not found.

    Searches activities on the same date (±1 day to cover timezone shifts) and matches
    on start time within half the swim's elapsed duration.
    """
    start_dt = _parse_strava_dt(activity["start_date"])
    elapsed_s: float = activity.get("elapsed_time", 0)
    tolerance_s = max(300.0, elapsed_s / 2)

    day_before = (start_dt - timedelta(days=1)).strftime("%Y-%m-%d")
    day_after = (start_dt + timedelta(days=1)).strftime("%Y-%m-%d")

    existing = garmin_client.get_activities_by_date(day_before, day_after)
    for a in existing:
        raw = a.get("startTimeGMT") or a.get("startTimeLocal", "")
        if not raw:
            continue
        try:
            garmin_dt = datetime.strptime(raw, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
            if abs(garmin_dt.timestamp() - start_dt.timestamp()) <= tolerance_s:
                return int(a["activityId"])
        except (ValueError, KeyError):
            continue
    return None


def is_duplicate_in_garmin(garmin_client: Garmin, activity: dict, activity_id: int) -> bool:
    try:
        garmin_id = _lookup_garmin_activity(garmin_client, activity)
        if garmin_id is not None:
            logger.info(
                "Strava activity %d matches existing Garmin activity %d — skipping.",
                activity_id,
                garmin_id,
            )
            return True
    except Exception as e:
        logger.warning("Could not check Garmin for duplicates for activity %d: %s — will attempt upload.", activity_id, e)
    return False


def upload_swim_to_garmin(garmin_client: Garmin, activity: dict, laps: list[dict], activity_id: int) -> None:
    """Build a FIT file from Strava data and upload it to Garmin Connect."""
    if is_duplicate_in_garmin(garmin_client, activity, activity_id):
        return

    fit_data = build_swim_fit(activity, laps)

    try:
        with tempfile.NamedTemporaryFile(suffix=".fit", delete=False) as tmp:
            tmp.write(fit_data)
            tmp_path = tmp.name
        try:
            with open(tmp_path, "rb") as fh:
                result = garmin_client.client.post(
                    "connectapi",
                    "/upload-service/upload/fit",
                    files={"file": (os.path.basename(tmp_path), fh, "application/octet-stream")},
                    headers={
                        "NK": "NT",
                        "origin": "https://sso.garmin.com",
                        "User-Agent": "GCM-iOS-5.7.2.1",
                    },
                    api=True,
                )
            logger.debug("Garmin import response: %s", result)

            strava_name = activity.get("name", "").strip()
            logger.info(
                "Uploaded Strava activity %d to Garmin Connect (%.0fm, %d laps).",
                activity_id,
                activity.get("distance", 0),
                len(laps),
            )

            if strava_name:
                # Garmin processes FIT files asynchronously; poll until the activity appears.
                garmin_id: int | None = None
                for attempt in range(6):
                    time.sleep(5.0)
                    try:
                        garmin_id = _lookup_garmin_activity(garmin_client, activity)
                    except Exception as e:
                        logger.warning("Error looking up Garmin activity (attempt %d/6): %s", attempt + 1, e)
                    if garmin_id is not None:
                        break

                if garmin_id is not None:
                    try:
                        garmin_client.set_activity_name(garmin_id, strava_name)
                        logger.info("Renamed Garmin activity %d to '%s'.", garmin_id, strava_name)
                    except Exception as e:
                        logger.warning("Failed to rename Garmin activity %d: %s", garmin_id, e)
                else:
                    logger.warning(
                        "Could not find Garmin activity for Strava ID %d after retries — not renamed.",
                        activity_id,
                    )

        finally:
            os.unlink(tmp_path)
    except Exception as e:
        logger.error("Failed to upload Strava activity %d to Garmin: %s", activity_id, e)
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description="Sync FORM swim workouts from Strava to Garmin Connect.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch swims from Strava but skip uploading to Garmin.",
    )
    args = parser.parse_args()

    client_id = os.environ["STRAVA_CLIENT_ID"]
    client_secret = os.environ["STRAVA_CLIENT_SECRET"]
    garmin_email = os.environ["GARMIN_EMAIL"]
    garmin_password = os.environ["GARMIN_PASSWORD"]

    last_synced_id = read_last_synced_id()
    logger.info("Last synced Strava activity ID: %d.", last_synced_id)

    refresh_token = os.environ["STRAVA_REFRESH_TOKEN"]
    access_token, new_refresh_token = refresh_strava_token(client_id, client_secret, refresh_token)
    # Persist the new token immediately — before any other network call
    write_strava_refresh_token(new_refresh_token)

    swims = fetch_strava_swims(access_token, last_synced_id)

    if not swims:
        logger.info("No new swims to sync.")
        return

    if args.dry_run:
        for swim in swims:
            logger.info(
                "[dry-run] Would upload Strava activity %d: '%s' (%.0fm).",
                swim["id"],
                swim.get("name", "Swim"),
                swim.get("distance", 0),
            )
        return

    if not os.path.exists(GARMIN_TOKEN_FILE):
        logger.error("Garmin token cache not found at %s — run get_garmin_token.py locally and add the file to your Gist.", GARMIN_TOKEN_FILE)
        raise RuntimeError(f"Missing Garmin token cache: {GARMIN_TOKEN_FILE}")
    logger.info("Garmin token cache found at %s.", GARMIN_TOKEN_FILE)

    garmin_client = Garmin(garmin_email, garmin_password)
    garmin_client.login(tokenstore=GARMIN_TOKEN_FILE)
    logger.info("Authenticated with Garmin Connect.")

    for swim in swims:
        activity_id = swim["id"]
        laps = fetch_strava_laps(activity_id, access_token)
        upload_swim_to_garmin(garmin_client, swim, laps, activity_id)
        write_last_synced_id(activity_id)

    logger.info("Sync complete. Last synced Strava activity ID: %d.", swims[-1]["id"])


if __name__ == "__main__":
    try:
        main()
    except KeyError as e:
        logger.error("Missing required environment variable: %s", e)
        sys.exit(1)
    except Exception as e:
        logger.error("Sync failed: %s", e)
        sys.exit(1)
