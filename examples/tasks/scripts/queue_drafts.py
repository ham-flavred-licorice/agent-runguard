#!/usr/bin/env python3
"""Put drafts in a review queue (here: a folder). With --check, verify every lead has a queued draft."""
import json
import shutil
import sys
from pathlib import Path

if __name__ == "__main__":
    run_dir = Path(sys.argv[1])
    queue = Path("out/review-queue")
    leads = [lead["id"] for lead in json.loads((run_dir / "leads.json").read_text(encoding="utf-8"))]
    if "--check" in sys.argv:
        missing = [lead for lead in leads if not (queue / f"{run_dir.name}-{lead}.md").is_file()]
        if missing:
            sys.exit(f"not queued: {', '.join(missing)}")
        print(f"all {len(leads)} drafts are queued")
    else:
        queue.mkdir(parents=True, exist_ok=True)
        for lead in leads:
            shutil.copyfile(run_dir / "drafts" / f"{lead}.md", queue / f"{run_dir.name}-{lead}.md")
        print(f"queued {len(leads)} drafts")
