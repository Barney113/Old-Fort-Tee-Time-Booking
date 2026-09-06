import os
import sys
import asyncio
import httpx
from datetime import datetime, timedelta

# Environment Configuration
GOLF_USERNAME = os.getenv("GOLF_USERNAME")
GOLF_PASSWORD = os.getenv("GOLF_PASSWORD")

# CPS / Golf Now API Endpoints (Adjust baseUrl if your course uses a custom tenant subdomain)
BASE_URL = "https://cps-api.clubprophet.com" 
ONLINE_API = f"{BASE_URL}/api/v1"

if not GOLF_USERNAME or not GOLF_PASSWORD:
    print("[!] Error: GOLF_USERNAME or GOLF_PASSWORD environment variables not set.")
    sys.exit(1)


async def get_authenticated_headers(client: httpx.AsyncClient) -> dict:
    """Authenticates against CPS API and returns authorized headers."""
    login_url = f"{ONLINE_API}/auth/login"
    payload = {
        "username": GOLF_USERNAME,
        "password": GOLF_PASSWORD
    }
    
    headers = {
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    }

    print("[*] Authenticating with Golf API...")
    res = await client.post(login_url, json=payload, headers=headers)
    
    if res.status_code != 200:
        raise RuntimeError(f"Authentication failed ({res.status_code}): {res.text}")
    
    data = res.json()
    token = data.get("token") or data.get("access_token")
    
    if not token:
        raise RuntimeError("Authentication succeeded but no token was returned.")
        
    print("[+] Successfully authenticated.")
    
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "User-Agent": headers["User-Agent"]
    }


async def execute_two_phase_booking(client: httpx.AsyncClient, headers: dict, slot: dict) -> bool:
    """Executes Phase 1 (Lock Slot) and Phase 2 (Confirm Booking) for a given slot."""
    slot_time = slot.get("startTime", "Unknown Time")
    slot_id = slot.get("id") or slot.get("teeTimeId")
    
    print(f"\n[*] ATTEMPTING LOCK on slot: {slot_time} (ID: {slot_id})")
    
    # Phase 1: Hold / Lock Tee Time
    lock_url = f"{ONLINE_API}/HoldTeeTime"
    lock_payload = {
        "teeTimeId": slot_id,
        "players": 4,
        "holes": 18
    }
    
    lock_res = await client.post(lock_url, json=lock_payload, headers=headers)
    if lock_res.status_code not in (200, 201):
        print(f"[!] Hold failed for {slot_time} ({lock_res.status_code}): {lock_res.text}")
        return False
        
    hold_data = lock_res.json()
    reservation_id = hold_data.get("reservationId") or hold_data.get("holdId")
    print(f"[+] SLOT LOCKED SUCCESSFULLY! Reservation ID: {reservation_id}")

    # Phase 2: Finalize / Confirm Booking
    confirm_url = f"{ONLINE_API}/ConfirmReservation"
    confirm_payload = {
        "reservationId": reservation_id,
        "players": 4,
        "holes": 18
    }
    
    confirm_res = await client.post(confirm_url, json=confirm_payload, headers=headers)
    if confirm_res.status_code in (200, 201):
        print(f"\n==================================================")
        print(f"[🎉] TEE TIME CONFIRMED & BOOKED FOR 4 PLAYERS!")
        print(f"     Time: {slot_time}")
        print(f"     Reservation ID: {reservation_id}")
        print(f"==================================================\n")
        return True
    else:
        print(f"[!] Confirmation failed ({confirm_res.status_code}): {confirm_res.text}")
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

                    # 1. Filter for 4-player capacity
                    four_player_slots = [
                        s for s in slots 
                        if s.get("maxPlayers", 4) >= 4 or s.get("pax", 4) >= 4
                    ]

                    if not four_player_slots:
                        print("[!] Sheet opened, but no 4-player slots found. Retrying in 2 seconds...")
                        await asyncio.sleep(2.0)
                        continue

                    # 2. Sort chronologically (earliest first)
                    four_player_slots.sort(key=lambda x: x.get("startTime"))

                    # 3. Execute two-phase reservation on earliest available
                    for slot in four_player_slots:
                        success = await execute_two_phase_booking(client, headers, slot)
                        if success:
                            return True
                        print(f"[!] Slot {slot.get('startTime')} taken. Trying next earliest...")

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
    # Targets 5 days out from current execution date
    target_date = (datetime.now() + timedelta(days=5)).strftime("%Y-%m-%d")
    print(f"[*] Target Booking Date set to: {target_date}")

    async with httpx.AsyncClient(timeout=10.0) as client:
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
