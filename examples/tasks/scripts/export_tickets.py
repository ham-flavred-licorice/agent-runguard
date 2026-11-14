#!/usr/bin/env python3
"""Stand-in for a real ticket export. Replace the body with a call to your help desk."""
import json
import sys

TICKETS = [
    {"id": "T-1041", "customer": "Northwind", "topic": "billing", "urgent": True, "text": "Charged twice for March."},
    {"id": "T-1042", "customer": "Acme", "topic": "login", "urgent": False, "text": "Password reset email never arrives."},
    {"id": "T-1043", "customer": "Globex", "topic": "billing", "urgent": False, "text": "Wants an invoice with a PO number."},
]

if __name__ == "__main__":
    with open(sys.argv[1], "w", encoding="utf-8") as fh:
        json.dump(TICKETS, fh, indent=1)
    print(f"exported {len(TICKETS)} tickets to {sys.argv[1]}")
