#!/usr/bin/env python3
"""monitor.py [--dry]  real Buffer status of every booked post → log/delivery.md.

Cache rule from GG Economics (23 Nov): posts already SENT never change, so they live in log/buffer_cache.json and are not
asked about again. Only scheduled/failed posts cost API calls. A failed post is recorded with triage (kind owner if
Instagram is disconnected). Without a key or in dry_run it lists what it would check."""
import sys, json
from common import Q, LOG, control, slots, now_utc, buffer_key
import buffer_api as B, triage

CACHE = LOG / "buffer_cache.json"
cache = json.loads(CACHE.read_text()) if CACHE.exists() else {}
dry = "--dry" in sys.argv or control()["mode"] != "auto" or not buffer_key()
rows = []
if not dry:   # 1 call: is the Instagram channel still connected? (GG lost a week of Reels to a silent disconnect)
    try:
        for c in B.gql('query($o:OrganizationId!){ channels(input:{organizationId:$o}){ id service isDisconnected isLocked isQueuePaused } }',
                       {"o": __import__("common").CFG["buffer_organization_id"]}, "channel health")["channels"]:
            if c["id"] in [v for v in __import__("common").CFG["buffer_channels"].values() if v] and (c["isDisconnected"] or c["isLocked"] or c["isQueuePaused"]):
                triage.record("channel", "health", f"{c['service']} channel isDisconnected={c['isDisconnected']} locked={c['isLocked']} paused={c['isQueuePaused']}")
                rows.append(("channel", "DISCONNECTED", f"reconnect the Laqta {c['service']} channel in Buffer"))
    except Exception as e:
        rows.append(("channel", "check failed", str(e)[:60]))
for s in slots():
    f = Q / s / "buffer_ids.json"
    if not f.exists(): continue
    j = json.loads(f.read_text()); ids = j.get("ids") or {"post": j.get("id")}
    for job, pid in ids.items():
        tag = f"{s} {job}"
        if cache.get(pid, {}).get("status") == "sent":
            rows.append((tag, "sent", cache[pid].get("sentAt", ""))); continue
        if dry:
            rows.append((tag, "would check", pid)); continue
        try:
            p = B.gql(B.POST_STATUS, {"id": pid}, f"status {tag}")["post"]
        except B.BudgetExceeded as e:
            rows.append((tag, "budget", str(e)[:60])); break
        cache[pid] = {"status": p["status"], "sentAt": p.get("sentAt"), "checked": now_utc().isoformat()}
        if p["status"] == "error":
            triage.record(s, f"delivery {job}", (p.get("error") or {}).get("message", "Buffer post error"))
        rows.append((tag, p["status"], p.get("sentAt") or p.get("dueAt")))
CACHE.write_text(json.dumps(cache, indent=1))
md = ["# Delivery (" + now_utc().strftime("%Y-%m-%d %H:%MZ") + ")", "", "| slot | status | when |", "|---|---|---|"]
md += [f"| {a} | {b} | {c} |" for a, b, c in rows]
(LOG / "delivery.md").write_text("\n".join(md) + "\n")
print("\n".join(md)); print(f"\nBuffer calls left (24 h): {B.calls_left()}")
