"""
Schwab API test script
----------------------
1. Logs in (first run: through the browser; later runs: from the saved token file)
2. Gets a quote for one symbol
3. Subscribes to Level 2 (NASDAQ_BOOK + NYSE_BOOK) and Level 1 for that symbol
   for 60 seconds, prints the first messages, and saves everything to a file.

The keys come from the app's settings.json (schwab_app_key, schwab_app_secret,
schwab_callback_url). The login token is kept in schwab_token.json, both next to this script.
After 7 days the token expires: delete schwab_token.json and run this again to log in.

Usage (in Command Prompt, from this folder):
    python schwab_test.py STLXF
"""

import asyncio
import json
import os
import sys
import time

from schwab.auth import client_from_manual_flow, client_from_token_file
from schwab.streaming import StreamClient

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SETTINGS_FILE = os.path.join(SCRIPT_DIR, "settings.json")
try:
    with open(SETTINGS_FILE, encoding="utf-8") as f:
        SETTINGS = json.load(f)
except (FileNotFoundError, ValueError) as e:
    sys.exit(f"Could not read {SETTINGS_FILE}: {e}")

APP_KEY = SETTINGS.get("schwab_app_key")
APP_SECRET = SETTINGS.get("schwab_app_secret")
CALLBACK_URL = SETTINGS.get("schwab_callback_url", "https://127.0.0.1")
TOKEN_PATH = os.path.join(SCRIPT_DIR, "schwab_token.json")

SYMBOL = sys.argv[1].upper() if len(sys.argv) > 1 else "STLXF"
LISTEN_SECONDS = 60
PRINT_FIRST_N = 2  # how many raw messages per service to print on screen
OUTPUT_FILE = os.path.join(SCRIPT_DIR, "schwab", f"stream_test_{SYMBOL}.jsonl")


def get_client():
    if not APP_KEY or not APP_SECRET:
        sys.exit(f"Missing schwab_app_key / schwab_app_secret in {SETTINGS_FILE}.")

    if os.path.exists(TOKEN_PATH):
        try:
            print("Using saved login token...")
            return client_from_token_file(TOKEN_PATH, APP_KEY, APP_SECRET)
        except Exception as e:
            print(f"Saved token could not be used ({e}). Logging in again.")

    print("\n=== Browser login ===")
    print("1. Open the link printed below and log in with your Schwab BROKERAGE login.")
    print("2. Approve access and select your account(s).")
    print("3. The browser will end on a page that fails to load (127.0.0.1) - that's expected.")
    print("4. Copy the FULL address from the browser's address bar and paste it here.\n")
    return client_from_manual_flow(APP_KEY, APP_SECRET, CALLBACK_URL, TOKEN_PATH)


def test_quote(client):
    print(f"\n=== Quote for {SYMBOL} ===")
    r = client.get_quote(SYMBOL)
    print(f"HTTP status: {r.status_code}")
    if r.status_code != 200:
        print(r.text)
        return
    data = r.json()
    q = data.get(SYMBOL, {}).get("quote", {})
    if not q:
        print(json.dumps(data, indent=2)[:2000])
        return
    print(f"Last:   {q.get('lastPrice')}")
    print(f"Bid:    {q.get('bidPrice')} x {q.get('bidSize')}")
    print(f"Ask:    {q.get('askPrice')} x {q.get('askSize')}")
    print(f"Volume: {q.get('totalVolume')}")


async def test_stream(client):
    print(f"\n=== Streaming {SYMBOL} for {LISTEN_SECONDS} seconds ===")
    stream = StreamClient(client)
    await stream.login()
    print("Streamer login OK.")

    counts = {"NASDAQ_BOOK": 0, "NYSE_BOOK": 0, "LEVELONE_EQUITIES": 0}
    out = open(OUTPUT_FILE, "w", encoding="utf-8")

    def make_handler(name):
        def handler(msg):
            counts[name] += 1
            out.write(json.dumps({"service": name, "received": time.time(), "msg": msg}) + "\n")
            out.flush()
            if counts[name] <= PRINT_FIRST_N:
                print(f"\n--- {name} message #{counts[name]} ---")
                print(json.dumps(msg, indent=2)[:3000])
        return handler

    stream.add_nasdaq_book_handler(make_handler("NASDAQ_BOOK"))
    stream.add_nyse_book_handler(make_handler("NYSE_BOOK"))
    stream.add_level_one_equity_handler(make_handler("LEVELONE_EQUITIES"))

    await stream.nasdaq_book_subs([SYMBOL])
    await stream.nyse_book_subs([SYMBOL])
    await stream.level_one_equity_subs([SYMBOL])
    print("Subscribed. Waiting for data...")

    end = time.time() + LISTEN_SECONDS
    while True:
        remaining = end - time.time()
        if remaining <= 0:
            break
        try:
            await asyncio.wait_for(stream.handle_message(), timeout=remaining)
        except asyncio.TimeoutError:
            break

    out.close()
    try:
        await stream.logout()
    except Exception:
        pass

    print("\n=== Summary ===")
    for name, n in counts.items():
        print(f"{name}: {n} messages")
    print(f"All messages saved to {OUTPUT_FILE}")
    if counts["NASDAQ_BOOK"] == 0 and counts["NYSE_BOOK"] == 0:
        print("No Level 2 data arrived for this symbol during the test.")


def main():
    client = get_client()
    test_quote(client)
    asyncio.run(test_stream(client))


if __name__ == "__main__":
    main()
