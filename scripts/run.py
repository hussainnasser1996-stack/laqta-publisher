#!/usr/bin/env python3
"""run.py [--dry]  the scheduled entry point (Mac launchd or GitHub Actions cron, see PLAN.md).

For every queued slot due within CONTROL.md book_ahead_hours (and not past by more than 6 h):
  skip if paused, in skip_dates, halted, already booked;
  run gates; if approved (or approval not required) → publish.py; failures go to triage.py.
Ends with a one-screen status board. In dry_run mode nothing leaves the machine."""
import sys, subprocess
from datetime import timedelta
from common import AP, Q, control, slots, load_post, due_utc, now_utc
import triage

PY = sys.executable
ctl = control()
if ctl["mode"] == "paused": sys.exit("paused (CONTROL.md)")
win = timedelta(hours=float(ctl.get("book_ahead_hours", 48)))
now = now_utc()
board = []
for s in slots():
    d = Q / s
    try: post = load_post(s); due = due_utc(post)
    except Exception as e: triage.record(s, "load", str(e)); board.append((s, "BROKEN", str(e)[:60])); continue
    if s[:10] in str(ctl.get("skip_dates", "")): board.append((s, "skip", "skip_dates")); continue
    if (d / "buffer_ids.json").exists(): board.append((s, "booked", post["scheduled_at"])); continue
    if (d / "halted").exists(): board.append((s, "HALTED", (d / "halted").read_text().strip()[:60])); continue
    if not (now - timedelta(hours=6) <= due <= now + win): board.append((s, "later", post["scheduled_at"])); continue
    g = subprocess.run([PY, str(AP / "scripts/gates.py"), s], capture_output=True, text=True)
    if g.returncode:
        triage.record(s, "gates", g.stdout[-1500:]); board.append((s, "GATES", g.stdout.strip().splitlines()[1][:70] if g.stdout.count("\n") > 1 else "")); continue
    args = [PY, str(AP / "scripts/publish.py"), s] + (["--dry"] if "--dry" in sys.argv else [])
    p = subprocess.run(args, capture_output=True, text=True)
    out = (p.stdout + p.stderr).strip()
    if p.returncode:
        k = triage.record(s, "publish", out); board.append((s, f"FAIL/{k}", out.splitlines()[-1][:60]))
    elif (d / "buffer_ids.json").exists():
        triage.resolve(s); board.append((s, "BOOKED", post["scheduled_at"]))
    else:
        board.append((s, "dry", "plan printed; see why_not_live")); print(out)
print("\nslot            status       detail")
for r in board: print(f"{r[0]:15} {r[1]:12} {r[2]}")
