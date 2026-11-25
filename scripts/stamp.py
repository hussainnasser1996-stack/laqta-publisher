#!/usr/bin/env python3
"""stamp.py humanizer <slot> <name>   record that <name> ran the humanizer on the current English caption
   stamp.py approve   <slot> <name>   human approval: writes queue/<slot>/approved (only if gates pass for this exact post.json)
   stamp.py unapprove <slot>

Both stamps hold a hash, so editing the caption (or post.json) after the stamp invalidates it."""
import sys, json, subprocess
from common import Q, sha, load_post, now_utc, AP

cmd, slot = sys.argv[1], sys.argv[2]
name = sys.argv[3] if len(sys.argv) > 3 else None
d = Q / slot
if cmd == "humanizer":
    p = load_post(slot)
    p["humanizer"] = {"checked": True, "caption_sha": sha(p["caption_en"] + p.get("yt_title", "") + p.get("li_doc_title", "")), "by": name, "at": now_utc().strftime("%Y-%m-%d")}
    (d / "post.json").write_text(json.dumps(p, ensure_ascii=False, indent=1) + "\n")
    print("humanizer stamped", slot)
elif cmd == "approve":
    if not name: sys.exit("approve needs a name (who approved it)")
    r = subprocess.run([sys.executable, str(AP / "scripts/gates.py"), slot])
    if r.returncode: sys.exit("gates fail: not approved")
    (d / "approved").write_text(json.dumps({"by": name, "at": now_utc().strftime("%Y-%m-%dT%H:%MZ"),
                                            "post_sha": sha((d / "post.json").read_text())}) + "\n")
    print("approved", slot, "by", name)
    subprocess.run([sys.executable, str(AP / "scripts/sync_public.py")])   # hand it to the public runner (laqta-publisher)
elif cmd == "unapprove":
    (d / "approved").unlink(missing_ok=True); print("unapproved", slot)
    subprocess.run([sys.executable, str(AP / "scripts/sync_public.py")])
else:
    sys.exit(__doc__)
