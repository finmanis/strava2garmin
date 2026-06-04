"""One-time script to authenticate with Garmin Connect and cache OAuth tokens."""
import os

from dotenv import load_dotenv
from garminconnect import Garmin

load_dotenv()

TOKEN_FILE = "./garmin_tokens.json"


def main() -> None:
    garmin_email = os.environ["GARMIN_EMAIL"]
    garmin_password = os.environ["GARMIN_PASSWORD"]

    def prompt_mfa() -> str:
        return input("Enter your Garmin MFA/2FA code: ").strip()

    print(f"Logging in to Garmin Connect as {garmin_email} ...")
    client = Garmin(garmin_email, garmin_password, prompt_mfa=prompt_mfa)
    client.login(tokenstore=TOKEN_FILE)

    print(f"\nTokens saved to {TOKEN_FILE}")
    print("Add this file to your GitHub Gist as: garmin_tokens.json")


if __name__ == "__main__":
    main()
