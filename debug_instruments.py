#!/usr/bin/env python3
"""
Debug script to inspect the actual CSV structure from INDstocks
"""
import csv
import io
from core.indstocks_auth import get_session

session = get_session()

# Fetch raw CSV
resp = session.session.get(
    f"{session.base_url}/market/instruments",
    params={"source": "index"},
    timeout=10,
    headers=session.session.headers
)
resp.raise_for_status()

# Show first 500 chars
print("Raw CSV (first 500 chars):")
print(resp.text[:500])
print("\n" + "="*60 + "\n")

# Parse and show columns
csv_data = csv.DictReader(io.StringIO(resp.text))
fieldnames = csv_data.fieldnames
print(f"CSV Columns: {fieldnames}")
print("\n" + "="*60 + "\n")

# Show first 5 rows with all fields
csv_data = csv.DictReader(io.StringIO(resp.text))
for i, row in enumerate(csv_data):
    if i >= 5:
        break
    print(f"Row {i}:")
    for key, val in row.items():
        if val:
            print(f"  {key}: {val}")
    print()
