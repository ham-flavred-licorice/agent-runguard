#!/usr/bin/env python3
"""Stand-in for a CRM query. Replace the body with a call to your CRM."""
import json
import sys

LEADS = [
    {"id": "L-207", "first_name": "Dana", "company": "Brightside Dental", "last_topic": "asked about onboarding time", "days_quiet": 6},
    {"id": "L-212", "first_name": "Omar", "company": "Kite Logistics", "last_topic": "requested pricing for 12 seats", "days_quiet": 9},
]

if __name__ == "__main__":
    with open(sys.argv[1], "w", encoding="utf-8") as fh:
        json.dump(LEADS, fh, indent=1)
    print(f"found {len(LEADS)} quiet leads")
