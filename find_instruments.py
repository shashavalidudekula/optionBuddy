#!/usr/bin/env python3
"""
find_instruments.py -- List available F&O instruments and their security_ids

Run with arguments:
  python find_instruments.py index     -- List all indices (Nifty50, BankNifty, etc)
  python find_instruments.py fno       -- List all F&O stocks
  python find_instruments.py equity    -- List all equity stocks
  python find_instruments.py search RELIANCE  -- Search for a symbol
"""
import sys
import csv
import io
from core.indstocks_auth import get_session

session = get_session()

def list_instruments(source):
    """Fetch and display instruments from INDstocks."""
    print(f"\nFetching {source} instruments from INDstocks...")

    try:
        resp = session.session.get(
            f"{session.base_url}/market/instruments",
            params={"source": source},
            timeout=10,
            headers=session.session.headers
        )
        resp.raise_for_status()

        # Parse CSV response (columns: EXCH, SEGMENT, SECURITY_ID)
        csv_data = csv.DictReader(io.StringIO(resp.text))
        instruments = list(csv_data)

        if not instruments:
            print(f"No instruments found for source: {source}")
            return

        print(f"\nFound {len(instruments)} instruments:\n")
        print(f"{'Instrument':<35} {'Exchange':<8} {'Security ID':<15}")
        print("-" * 60)

        for inst in instruments:
            segment = inst.get('SEGMENT', '').strip()
            exch = inst.get('EXCH', '').strip()
            sec_id = inst.get('SECURITY_ID', '').strip()
            if segment and sec_id:
                print(f"{segment:<35} {exch:<8} {sec_id:<15}")

        # Generate .env config
        print(f"\n{'='*60}")
        print("Add to .env:")
        print(f"{'='*60}")

        if source == "index":
            ids = [inst.get('SECURITY_ID', '').strip() for inst in instruments if inst.get('SECURITY_ID')]
            print(f"TRACKED_INDICES={','.join(ids)}")
        elif source == "fno":
            ids = [inst.get('SECURITY_ID', '').strip() for inst in instruments if inst.get('SECURITY_ID')]
            print(f"TRACKED_STOCKS={','.join(ids[:20])}")  # First 20 to avoid too long line
        elif source == "equity":
            ids = [inst.get('SECURITY_ID', '').strip() for inst in instruments if inst.get('SECURITY_ID')]
            print(f"TRACKED_STOCKS={','.join(ids[:20])}")

    except Exception as e:
        print(f"Error fetching instruments: {e}")
        import traceback
        traceback.print_exc()

def search_instruments(symbol):
    """Search for a specific symbol across all sources."""
    print(f"\nSearching for '{symbol}'...")

    found = []
    for source in ["index", "fno", "equity"]:
        try:
            resp = session.session.get(
                f"{session.base_url}/market/instruments",
                params={"source": source},
                timeout=10,
                headers=session.session.headers
            )
            resp.raise_for_status()

            csv_data = csv.DictReader(io.StringIO(resp.text))
            for inst in csv_data:
                if symbol.upper() in inst.get('SEGMENT', '').upper():
                    found.append((source, inst))
        except Exception as e:
            pass

    if not found:
        print(f"Symbol '{symbol}' not found")
        return

    print(f"\nFound {len(found)} matches:")
    print(f"\n{'Source':<10} {'Instrument':<35} {'Security ID':<15}")
    print("-" * 65)

    for source, inst in found:
        instrument = inst.get('SEGMENT', '').strip()
        sec_id = inst.get('SECURITY_ID', '').strip()
        print(f"{source:<10} {instrument:<35} {sec_id:<15}")

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print(__doc__)
        print("\nExample usage:")
        print("  python find_instruments.py index")
        print("  python find_instruments.py fno")
        print("  python find_instruments.py search NIFTY")
        sys.exit(1)

    cmd = sys.argv[1].lower()

    if cmd == "search" and len(sys.argv) > 2:
        search_instruments(sys.argv[2])
    elif cmd in ["index", "fno", "equity"]:
        list_instruments(cmd)
    else:
        print(f"Unknown command: {cmd}")
        print(__doc__)
