import os
import sys
import asyncio
import httpx
from datetime import datetime, timedelta

# Environment Configuration
GOLF_USERNAME = os.getenv("GOLF_USERNAME")
GOLF_PASSWORD = os.getenv("GOLF_PASSWORD")

# CPS / Old Fort Endpoints
BASE_URL = "https://oldfort.cps.golf"
IDENTITY_URL = f"{BASE_URL}/identityapi"
ONLINE_API = f"{BASE_URL}/onlineres/onlineapi/api/v1/onlinereservation"

if not GOLF_USERNAME or not GOLF_PASSWORD:
    print("[!] Error: GOLF_USERNAME or GOLF_PASSWORD environment variables not set.")
    sys.exit(1)


async def get_authenticated_headers(client: httpx.AsyncClient) -> dict:
    login_url = f"{IDENTITY_URL}/connect/token"

    login_payload = {
        "grant_type": "password",
        "username": GOLF_USERNAME,
        "password": GOLF_PASSWORD,
        "client_id": "js1",
        "client_secret": "v4secret",
        "scope": "openid profile onlinereservation sale inventory sh customer email recommend references",
    }

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/x-www-form-urlencoded",
        "Origin": BASE_URL,
        "Referer": f"{BASE_URL}/onlineresweb/search-teetime",
    }

    print("[*] Authenticating with CPS IdentityServer...")
    res = await client.post(login_url, params=login_payload, data=login_payload, headers=headers)

    if res.status_code != 200:
        raise RuntimeError(f"Authentication failed ({res.status_code}): {res.text}")

    data = res.json()
    token = data.get("access_token")

    if not token:
        raise RuntimeError("Authentication succeeded but no access_token was returned.")

    print("[+] Successfully authenticated.")

    return {
        "User-Agent": headers["User-Agent"],
        "Accept": "application/json, text/plain, */*",
        "Authorization": f"Bearer {token}",
        "Origin": BASE_URL,
        "Referer": f"{BASE_URL}/onlineresweb/search-teetime",
    }


async def execute_two_phase_booking(client: httpx.AsyncClient, headers: dict, selected_slot: dict) -> bool:
    slot_time = selected_slot.get("startTime", "Unknown Time")

    print(f"\n[*] ATTEMPTING LOCK on slot: {slot_time}")

    lock_url = f"{ONLINE_API}/LockTeeTime"
    lock_payload = {
        "teeSheetId": selected_slot.get("teeSheetId"),
        "holes": 18,
        "pax": 4,
        "time": slot_time,
    }

    lock_res = await client.post(lock_url, json=lock_payload, headers=headers)
    if lock_res.status_code != 200:
        print(f"[!] Lock rejected ({lock_res.status_code}): {lock_res.text}")
        return False

    lock_data = lock_res.json()
    locked_session_id = lock_data.get("lockedTeeTimesSessionId")
    booking_tx_id = lock_data.get("bookingTransactionId")
    tx_id = lock_data.get("transactionId")

    if not locked_session_id:
        print("[!] Lock response missing lockedTeeTimesSessionId.")
        return False

    print(f"[+] SLOT LOCKED! Session: {locked_session_id}")
    print(f"[*] Lock response dump: {lock_data}")

    confirm_url = f"{ONLINE_API}/ReserveTeeTimes"
    reserve_payload = {
        "affiliateId": None,
        "bookingTransactionId": booking_tx_id,
        "cancelReservationLink": f"{BASE_URL}/onlineresweb/auth/verify-email?returnUrl=cancel-booking",
        "finalizeSaleModel": {
            "acct": "10000000000000000000000000006",
            "playerId": 0,
            "isGuest": False,
        },
        "homePageLink": f"{BASE_URL}/onlineresweb/",
        "lockedTeeTimesSessionId": locked_session_id,
        "sessionGuid": None,
        "transactionId": tx_id,
    }

    print(f"[*] Reserve payload: {reserve_payload}")

    confirm_res = await client.post(confirm_url, json=reserve_payload, headers=headers)
    if confirm_res.status_code == 200:
        print(f"\n==================================================")
        print(f"[SUCCESS] TEE TIME BOOKED!")
        print(f"     Time: {slot_time}")
        print(f"==================================================\n")
        return True
    else:
        print(f"[!] Final reservation failed ({confirm_res.status_code}): {confirm_res.text}")
        return False


async def main():
    target_date = (datetime.now() + timedelta(days=7)).strftime("%Y-%m-%d")
    print(f"[*] TEST MODE: Targeting earliest available slot on {target_date}")

    async with httpx.AsyncClient(timeout=10.0, verify=False) as client:
        try:
            headers = await get_authenticated_headers(client)

            search_url = f"{ONLINE_API}/GetTeeTimes?date={target_date}&holes=18"
            print(f"[*] Fetching tee times for {target_date}...")
            res = await client.get(search_url, headers=headers)

            print(f"[*] GetTeeTimes status: {res.status_code}")
            print(f"[*] GetTeeTimes response: {res.text[:500]}")

            if res.status_code != 200:
                print(f"[!] Failed to fetch tee times: {res.text}")
                sys.exit(1)

            slots = res.json()

            if not slots or not isinstance(slots, list) or len(slots) == 0:
                print(f"[!] No tee times available for {target_date}. Exiting.")
                sys.exit(0)

            print(f"[+] Found {len(slots)} total slots.")

            four_player_slots = [
                s for s in slots
                if s.get("maxPlayers", 0) >= 4 or s.get("pax", 0) >= 4
            ]

            print(f"[+] {len(four_player_slots)} slots with 4-player capacity.")

            if not four_player_slots:
                print("[!] No 4-player slots available. Dumping first 3 raw slots for debugging:")
                for s in slots[:3]:
                    print(f"    {s}")
                sys.exit(0)

            four_player_slots.sort(key=lambda x: x.get("startTime", ""))
            earliest = four_player_slots[0]
            print(f"[*] Earliest 4-player slot: {earliest.get('startTime')}")

            success = await execute_two_phase_booking(client, headers, earliest)

            if not success:
                print("\n[-] Test booking failed — check the error output above.")

        except Exception as e:
            print(f"\n[!] Critical Error: {e}")
            sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
