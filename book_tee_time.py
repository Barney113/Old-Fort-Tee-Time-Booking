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
    """Authenticates against CPS IdentityServer using URL parameters for client credentials."""
    login_url = f"{IDENTITY_URL}/connect/token"
    
    # CPS IdentityServer expects token credentials via URL parameters
    login_payload = {
        "grant_type": "password",
        "username": GOLF_USERNAME,
        "password": GOLF_PASSWORD,
        "client_id": "cps-web",
        "client_secret": "secret",
        "scope": "openid profile email onlinereservation",
    }
    
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Content-Type": "application/x-www-form-urlencoded",
        "Origin": BASE_URL,
        "Referer": f"{BASE_URL}/onlineresweb/search-teetime",
    }

    print("[*] Authenticating with CPS IdentityServer...")
    # Send login_payload as query params and form data to satisfy strict CPS endpoint routing
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
    """Executes Phase 1 (LockTeeTime) and Phase 2 (ReserveTeeTimes) for a selected slot."""
    slot_time = selected_slot.get("startTime", "Unknown Time")
    
    print(f"\n[*] ATTEMPTING LOCK on slot: {slot_time}")
    
    # Phase 1: Lock Tee Time
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

    print(f"[+] SLOT LOCKED SUCCESSFULLY! Session: {locked_session_id}")

    # Phase 2: Finalize Reservation
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
    
    confirm_res = await client.post(confirm_url, json=reserve_payload, headers=headers)
    if confirm_res.status_code == 200:
        print(f"\n==================================================")
        print(f"[🎉] TEE TIME CONFIRMED & BOOKED FOR 4 PLAYERS!")
        print(f"     Time: {slot_time}")
        print(f"==================================================\n")
        return True
    else:
        print(f"[!] Final reservation failed ({confirm_res.status_code}): {confirm_res.text}")
        return False


async def adaptive_poll_and_book(client: httpx.AsyncClient, headers: dict, target_date: str):
    """Monitors the API indefinitely with low-frequency requests until tee times drop,
    then locks the earliest 4-player slot immediately.
    """
    search_url = f"{ONLINE_API}/GetTeeTimes?date={target_date}&holes=18"

    print(f"[*] Starting Passive Monitor for {target_date} (Checking every 30s)...")
    
    poll_count = 0

    while True:
        try:
            poll_count += 1
            now_str = datetime.now().strftime("%H:%M:%S")
            res = await client.get(search_url, headers=headers)

            if res.status_code == 200:
                slots = res.json()

                # TEE SHEET UNLOCKED: Non-empty slot list returned!
                if slots and isinstance(slots, list) and len(slots) > 0:
                    print(f"\n[!] TEE SHEET OPENED AT {now_str}! (Attempt #{poll_count})")

                    # Filter for 4-player capacity
                    four_player_slots = [
                        s for s in slots 
                        if s.get("maxPlayers", 4) >= 4 or s.get("pax", 4) >= 4
                    ]

                    if not four_player_slots:
                        print("[!] Sheet opened, but no 4-player slots found. Retrying in 2 seconds...")
                        await asyncio.sleep(2.0)
                        continue

                    # Sort chronologically (earliest first)
                    four_player_slots.sort(key=lambda x: x.get("startTime"))

                    # Execute two-phase reservation on earliest available
                    for slot in four_player_slots:
                        success = await execute_two_phase_booking(client, headers, slot)
                        if success:
                            return True
                        print(f"[!] Slot {slot.get('startTime')} taken or locked. Retrying next earliest...")

            elif res.status_code == 401:
                # Token expired during long-running poll -> Refresh headers
                print(f"\n[{now_str}] Token expired during monitoring. Re-authenticating...")
                headers = await get_authenticated_headers(client)

            print(f"[{now_str}] Checked (Attempt #{poll_count}): Sheet locked. Sleeping 30s...", end="\r")
            
            # Passive interval: 30 seconds
            await asyncio.sleep(30.0)

        except Exception as e:
            print(f"\n[!] Monitoring network exception: {e}")
            await asyncio.sleep(10.0)


async def main():
    target_date = (datetime.now() + timedelta(days=5)).strftime("%Y-%m-%d")
    print(f"[*] Target Booking Date set to: {target_date}")

    async with httpx.AsyncClient(timeout=10.0, verify=False) as client:
        try:
            headers = await get_authenticated_headers(client)
            booked = await adaptive_poll_and_book(client, headers, target_date)
            
            if not booked:
                print("\n[-] Monitoring ended without successful booking.")
        except Exception as e:
            print(f"\n[!] Critical Error: {e}")
            sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
