#!/usr/bin/env python3
"""publish.py <slot> [--dry]

Books ONE queued post in Buffer: Reels → Instagram Reel + YouTube Short + LinkedIn video (same video); carousels → Instagram
carousel + LinkedIn document (PDF of the slides); Stories → Instagram only. Adapted from GG Economics'
publish.py (createPost over GraphQL, 429 backoff, call budget, duplicate guard, buffer_ids.json "never twice" guard).

It refuses unless ALL of these hold:
  1. CONTROL.md mode is `auto` (dry_run/paused → prints the plan and exits 0)
  2. gates pass for this exact post.json (gates_passed hash)
  3. approval_required → queue/<slot>/approved exists with the same post.json hash (scripts/stamp.py approve)
  4. config.json has buffer_organization_id + buffer_channels.instagram, a key in the keychain, and a media host
  5. queue/<slot>/buffer_ids.json does not exist yet

Dry-run (the default until all of that is set up) prints the exact createPost input it would send.
Writes queue/<slot>/buffer_ids.json on success, publish_failed on failure."""
import sys, json, shutil, subprocess, hashlib
from common import AP, Q, CFG, control, sha, load_post, compose_caption, due_utc, now_utc, buffer_key, youtube_captions, linkedin_text, LI_PDF
from datetime import timedelta
import buffer_api as B


def jobs_for(post, urls, due_s, li_pdf_url=None, li_video_url=None):
    """One Buffer createPost input per target channel. Reels → Instagram Reel + YouTube Short + LinkedIn video (same 9:16 video);
    carousels → Instagram carousel + LinkedIn document; Stories → Instagram only. A target whose channel id is null is
    listed as 'not connected'. LinkedIn = the Laqta company page only (config.json buffer_channels.linkedin)."""
    ch = CFG["buffer_channels"]; t = post["type"]; jobs, skipped = [], []
    text = compose_caption(post)[: CFG["caption_max_chars"]]
    base = lambda cid: {"channelId": cid, "schedulingType": "automatic", "mode": "customScheduled", "dueAt": due_s}
    if t == "reel":
        jobs.append(("instagram_reel", ch.get("instagram"), {**base(ch.get("instagram")), "text": text,
            "assets": [{"video": {"url": urls[0], "metadata": {"thumbnailOffset": post.get("thumbnail_offset_ms", 1500)}}}],
            "metadata": {"instagram": {"type": "reel", "shouldShareToFeed": True, "isAiGenerated": False}}}))
        yt_tags = (post.get("hashtags", [])[:2] + ["#Shorts"])
        yen, yar = youtube_captions(post)                 # YouTube has no DMs: "DM us" → WhatsApp / laqta-booths.com
        desc = "\n\n".join(x for x in [yen.strip(), yar.strip(), " ".join(yt_tags)] if x)
        jobs.append(("youtube_short", ch.get("youtube"), {**base(ch.get("youtube")), "text": desc[:4900],
            "assets": [{"video": {"url": urls[0]}}],
            "metadata": {"youtube": {"title": post.get("yt_title", "")[: CFG.get("youtube_title_max", 60)], "categoryId": CFG.get("youtube_category_id", "22"),
                                     "privacy": "public", "madeForKids": False, "notifySubscribers": True}}}))
        jobs.append(("linkedin_video", ch.get("linkedin"), {**base(ch.get("linkedin")), "text": linkedin_text(post)[:2900],
            "assets": [{"video": {"url": li_video_url or urls[0]}}]}))       # the square cut (post.json linkedin_media)
    elif t == "carousel":
        jobs.append(("instagram_carousel", ch.get("instagram"), {**base(ch.get("instagram")), "text": text,
            "assets": [{"image": {"url": u}} for u in urls], "metadata": {"instagram": {"type": "post", "shouldShareToFeed": True}}}))
        jobs.append(("linkedin_document", ch.get("linkedin"), {**base(ch.get("linkedin")), "text": linkedin_text(post)[:2900],
            "assets": [{"document": {"url": li_pdf_url, "title": post.get("li_doc_title", "")[: CFG.get("linkedin_doc_title_max", 60)],
                                     "thumbnailUrl": urls[0]}}]}))
    else:
        a = [{"video": {"url": urls[0]}}] if urls[0].endswith(".mp4") else [{"image": {"url": urls[0]}}]
        jobs.append(("instagram_story", ch.get("instagram"), {**base(ch.get("instagram")), "text": "", "assets": a,
            "metadata": {"instagram": {"type": "story"}}}))
    live = [(n, inp) for n, cid, inp in jobs if cid]
    skipped = [n for n, cid, _ in jobs if not cid]
    return live, skipped


def plan(slot):
    post = load_post(slot); d = Q / slot
    ctl = control()
    reasons = []
    if ctl["mode"] != "auto": reasons.append(f"CONTROL.md mode is {ctl['mode']}")
    if "--dry" in sys.argv: reasons.append("--dry")
    gp = d / "gates_passed"
    if not gp.exists() or gp.read_text().strip() != sha((d / "post.json").read_text()):
        reasons.append("gates have not passed for this version of post.json (run scripts/gates.py)")
    if ctl.get("approval_required", True):
        ap = d / "approved"
        if not ap.exists(): reasons.append("not approved (scripts/stamp.py approve <slot> <name>)")
        elif json.loads(ap.read_text()).get("post_sha") != sha((d / "post.json").read_text()):
            reasons.append("post.json changed after approval: approve again")
    if post.get("publish_via") == "manual": reasons.append("publish_via=manual (post by hand)")
    if not CFG.get("buffer_organization_id"): reasons.append("buffer_organization_id is null in config.json")
    if not buffer_key(): reasons.append("no Buffer API key in keychain 'laqta-buffer'")
    if not CFG["media_host"].get("kind"): reasons.append("no public media host configured (config.json media_host)")
    booked = json.loads((d / "buffer_ids.json").read_text()).get("ids", {}) if (d / "buffer_ids.json").exists() else {}
    due = due_utc(post)
    if due < now_utc() + timedelta(minutes=4):
        due = now_utc() + timedelta(minutes=5)                    # built late: post in 5 min (same as GG)
    due_s = due.strftime("%Y-%m-%dT%H:%M:00.000Z")
    urls = [media_url(slot, m["path"]) for m in post["media"]]
    li_pdf_url = media_url(slot, LI_PDF) if post["type"] == "carousel" else None
    li_video_url = media_url(slot, post["linkedin_media"]) if post["type"] == "reel" and post.get("linkedin_media") else None
    live, skipped = jobs_for(post, urls, due_s, li_pdf_url, li_video_url)
    live = [(n, inp) for n, inp in live if n not in booked]       # book only what isn't booked yet (e.g. Instagram added later)
    if not live and not booked: reasons.append(f"no connected channel for a {post['type']} (not connected: {', '.join(skipped)})")
    if not live and booked: sys.exit(f"{slot}: already booked on every connected channel ({', '.join(booked)})")
    return post, live, skipped, booked, reasons, due_s


def media_url(slot, rel):
    h = CFG["media_host"]
    p = (Q / slot / rel).resolve()
    tag = hashlib.sha1(p.read_bytes()).hexdigest()[:8] if p.exists() else "missing"
    name = f"{slot}-{tag}-{p.name}"
    return f"{h['base_url'].rstrip('/')}/{name}" if h.get("base_url") else f"<media host>/{name}"


def host_media(slot, post):
    """github_raw: copy into the local clone of a PUBLIC media repo, push, and check every URL answers 200 (GG's method)."""
    h = CFG["media_host"]
    if h["kind"] != "github_raw": sys.exit(f"media host kind {h['kind']} not implemented")
    from pathlib import Path
    import os
    repo = Path(os.path.expanduser(h["repo_local"]))
    files = [m["path"] for m in post["media"]] + ([LI_PDF] if post["type"] == "carousel" else []) + \
            ([post["linkedin_media"]] if post["type"] == "reel" and post.get("linkedin_media") else [])
    for rel in files:
        p = (Q / slot / rel).resolve()
        shutil.copy(p, repo / media_url(slot, rel).rsplit("/", 1)[1])
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-qm", f"media {slot}"], check=False)
    subprocess.run(["git", "-C", str(repo), "push", "-q"], check=True)
    for rel in files:
        u = media_url(slot, rel)
        code = subprocess.run(["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}", "-I", u], capture_output=True, text=True).stdout
        if code != "200": raise RuntimeError(f"media not reachable yet: {u} -> {code}")


def main():
    slot = next(a for a in sys.argv[1:] if not a.startswith("--"))
    post, live, skipped, booked, reasons, due_s = plan(slot)
    if reasons:
        print(json.dumps({"slot": slot, "type": post["type"], "due_utc": due_s, "dry": True, "why_not_live": reasons,
                          "targets": [n for n, _ in live], "not_connected": skipped, "already_booked": list(booked),
                          "createPost_inputs": {n: inp for n, inp in live}}, ensure_ascii=False, indent=1))
        print(f"  Buffer calls left in the last 24 h: {B.calls_left()}")
        return 0
    d = Q / slot; ids = dict(booked); failed = {}
    try:
        host_media(slot, post)
        try:   # never book a second identical post (same channel, slot, text); costs 1 call
            ex = B.gql(B.SCHEDULED, {"o": CFG["buffer_organization_id"]}, "list scheduled")["posts"]["edges"]
            existing = {(e["node"]["channelId"], e["node"]["dueAt"][:16], (e["node"]["text"] or "")[:80]): e["node"]["id"] for e in ex}
        except B.BudgetExceeded: raise
        except Exception as e:
            existing = {}; print("duplicate check failed (continuing):", e)
        for name, inp in live:
            k = (inp["channelId"], inp["dueAt"][:16], inp["text"][:80])
            if k in existing: ids[name] = existing[k]; print(name, "already scheduled in Buffer as", existing[k]); continue
            res = B.gql(B.CREATE_POST, {"i": inp}, f"createPost {slot} {name}")["createPost"]
            if res["__typename"] == "PostActionSuccess": ids[name] = res["post"]["id"]
            else: failed[name] = res.get("message") or res["__typename"]; print("Buffer rejected", name, failed[name])
    except Exception as e:
        failed["_run"] = f"{type(e).__name__}: {e}"
    if ids:
        (d / "buffer_ids.json").write_text(json.dumps({"due": due_s, "ids": ids, "type": post["type"], "not_connected": skipped,
                                                       "booked_at": now_utc().isoformat(), **({"failed": failed} if failed else {})}, indent=1) + "\n")
    if failed:
        (d / "publish_failed").write_text(f"{now_utc().isoformat()} {json.dumps(failed)}\n"); print("FAILED", slot, failed); return 1
    (d / "publish_failed").unlink(missing_ok=True); print("scheduled", slot, ids, due_s); return 0


if __name__ == "__main__":
    sys.exit(main())
