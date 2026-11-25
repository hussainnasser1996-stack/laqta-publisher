#!/usr/bin/env python3
"""gates.py <slot> [<slot> ...] | gates.py --all [--no-arabic-run]

Checks a queued post before it can be approved or booked. Writes queue/<slot>/gates_passed (with the post's hash)
or queue/<slot>/gates_failed (the reasons). Exit 1 if any slot fails.

FAIL on:
  - post.json missing fields, bad type, scheduled_at without a timezone
  - media missing on disk, or media_status not "ready"
  - a render/drawing without label_onscreen=true, or a post with renders whose captions lack the visualisation line
  - Arabic caption not passing the arabic-newsroom checker (verdict PASS); result cached in ar_gate.json by caption hash,
    so a Linux runner without the checker can still verify a caption the Mac already passed
  - English caption not humanizer-checked (post.json humanizer.checked + caption_sha must match the current caption)
  - more than 5 hashtags, caption over 2,200 characters
  - a banned word (Al Aali, العالي, pilot, negotiable, ...) or banned seating claim anywhere in the copy
  - a venue named without venue_confirmed_in_writing=true
WARN (does not block): missing tagline, AI-writing tells in English, reel not 1080x1920 or outside 3-90 s.
"""
import sys, json, re, subprocess, shutil, os, hashlib
from common import AP, Q, CFG, TYPES, sha, load_post, slots, now_utc, due_utc
from datetime import datetime

REQUIRED = ["type", "pillar", "scheduled_at", "caption_en", "caption_ar", "hashtags", "media", "media_status"]
AI_TELLS = [r"—", r"\bnot just\b", r"\bit'?s not\b.*\bit'?s\b", r"\bdelve\b", r"\bgame[- ]changer\b", r"\belevate\b",
            r"\bseamless(ly)?\b", r"\bunleash\b", r"\bin today's\b", r"\bmore than just\b", r"\btestament\b"]


def arabic_gate(slot, text, run_checker=True, stamp_name="ar_gate.json"):
    """Return (ok, detail). Uses the cached result when the caption hash matches."""
    stamp = Q / slot / stamp_name
    h = sha(text)
    if stamp.exists():
        s = json.loads(stamp.read_text())
        if s.get("caption_sha") == h:
            return s.get("verdict") == "PASS", f"cached {s.get('verdict')} ({s.get('at')})"
    chk = os.path.expanduser(CFG["arabic_checker"]); py = os.path.expanduser(CFG["arabic_checker_python"])
    if not run_checker or not os.path.exists(chk):
        return False, "Arabic not checked yet and the arabic-newsroom checker is not on this machine (run gates.py on the Mac)"
    if not os.path.exists(py): py = sys.executable
    tmp = Q / slot / ".caption_ar.txt"; tmp.write_text(text)
    r = subprocess.run([py, chk, str(tmp), "--no-camel"], capture_output=True, text=True, stdin=subprocess.DEVNULL)
    tmp.unlink(missing_ok=True)
    out = (r.stdout + r.stderr).strip()
    m = re.search(r"verdict (\w+)", out)
    verdict = m.group(1) if m else "ERROR"
    stamp.write_text(json.dumps({"caption_sha": h, "verdict": verdict, "at": now_utc().strftime("%Y-%m-%dT%H:%MZ"),
                                 "summary": out.splitlines()[-1][:200] if out else ""}, ensure_ascii=False, indent=1))
    return verdict == "PASS", out.splitlines()[-1][:200] if out else "no output"


def media_probe(p):
    if not shutil.which("ffprobe"): return None
    r = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height:format=duration",
                        "-of", "json", str(p)], capture_output=True, text=True)
    try:
        j = json.loads(r.stdout); s = j["streams"][0]
        return s["width"], s["height"], float(j["format"].get("duration") or 0)
    except Exception:
        return None


def sting_match(video, sting_wav, tail=4.0):
    """Normalised cross-correlation of the Laqta snap against the last `tail` seconds of the video's audio (0..1)."""
    import numpy as np
    def pcm(args):
        r = subprocess.run(["ffmpeg", "-v", "error", *args, "-f", "f32le", "-ac", "1", "-ar", "16000", "-"], capture_output=True)
        return np.frombuffer(r.stdout, dtype="<f4").astype(float)
    a = pcm(["-sseof", f"-{tail}", "-i", str(video)]); b = pcm(["-i", str(sting_wav)])
    if len(a) < len(b) or not len(b): return 0.0
    n = 1 << int(np.ceil(np.log2(len(a) + len(b))))
    cc = np.fft.irfft(np.fft.rfft(a, n) * np.conj(np.fft.rfft(b, n)), n)[: len(a) - len(b) + 1]
    e = np.sqrt(np.convolve(a ** 2, np.ones(len(b)), "valid") * (b ** 2).sum()) + 1e-12
    return float(np.max(cc / e))


def logo_bug_scores(video, tmpl_path, widths, end_ident_s):
    """Max normalised template-match score of the logo bug in frames at 0, 25, 50, 75 % of the content part."""
    import cv2, numpy as np
    tpl = cv2.imread(str(tmpl_path)); r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(video)],
                                                       capture_output=True, text=True)
    content = max(0.5, float(r.stdout.strip() or 0) - end_ident_s); out = []
    for ts in [0.0, content * .25, content * .5, content * .75]:
        p = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{ts:.3f}", "-i", str(video), "-frames:v", "1", "-f", "image2pipe", "-vcodec", "png", "-"], capture_output=True).stdout
        fr = cv2.imdecode(np.frombuffer(p, np.uint8), cv2.IMREAD_COLOR)
        if fr is None: out.append(0.0); continue
        fr = fr[: fr.shape[0] // 3]                       # the bug lives in the top third
        best = 0.0
        for w in widths:
            t = cv2.resize(tpl, (w, round(w * tpl.shape[0] / tpl.shape[1])), interpolation=cv2.INTER_AREA)
            if t.shape[0] < fr.shape[0] and t.shape[1] < fr.shape[1]:
                best = max(best, float(cv2.matchTemplate(fr, t, cv2.TM_CCOEFF_NORMED).max()))
        out.append(round(best, 3))
    return out


def check(slot, run_checker=True):
    fails, warns = [], []
    try:
        post = load_post(slot)
    except Exception as e:
        return [f"post.json unreadable: {e}"], []
    for k in REQUIRED:
        if k not in post: fails.append(f"missing field: {k}")
    if fails: return fails, warns
    t = post["type"]
    if t not in TYPES: fails.append(f"type must be one of {TYPES}, got {t}")
    try:
        dt = datetime.fromisoformat(post["scheduled_at"])
        if dt.tzinfo is None: fails.append("scheduled_at needs a timezone, e.g. 2026-11-29T19:00:00+03:00 (Bahrain)")
    except Exception:
        fails.append(f"scheduled_at not ISO: {post['scheduled_at']}")

    # media
    if post["media_status"] != "ready":
        fails.append(f"media_status is '{post['media_status']}': {post.get('brief', '')[:120]}")
    media = post["media"]
    if not media and post["media_status"] == "ready": fails.append("no media listed")
    for m in media:
        p = (Q / slot / m["path"]).resolve()
        if post["media_status"] == "ready" and not p.exists(): fails.append(f"media file missing: {m['path']}")
        if m.get("kind") in CFG["media_kinds_needing_label"] and not m.get("label_onscreen"):
            fails.append(f"{m['path']}: kind={m.get('kind')} needs the on-screen label '{CFG['visualisation_label']}' (label_onscreen: true)")
        if "kind" not in m: fails.append(f"{m['path']}: media kind missing (render|drawing|photo|footage|graphic)")
        if p.exists() and t in ("reel", "story") and p.suffix.lower() in (".mp4", ".mov"):
            pr = media_probe(p)
            if pr:
                w, h, d = pr
                if (w, h) != (1080, 1920): warns.append(f"{m['path']}: {w}x{h}, expected 1080x1920")
                if t == "reel" and not 3 <= d <= 90: warns.append(f"{m['path']}: {d:.1f} s, Reels should be 3-90 s")
    lb = CFG.get("logo_bug")
    if lb and t in ("reel", "story") and any(str(m["path"]).endswith((".mp4", ".mov")) for m in media):
        if not post.get("logo_bug"):
            fails.append("video must carry the Laqta logo bug from frame 0 (set logo_bug: true after adding it)")
        elif post["media_status"] == "ready":
            v = Q / slot / next(m["path"] for m in media if str(m["path"]).endswith((".mp4", ".mov")))
            if v.exists():
                sc = logo_bug_scores(v, AP / lb["template"], lb["widths_px"], lb["end_ident_s"])
                if min(sc) < lb["min_match"]: fails.append(f"logo bug not found in every sampled frame (scores {sc}, need >= {lb['min_match']})")
    if t == "reel" and CFG["buffer_channels"].get("linkedin") and post["media_status"] == "ready":   # LinkedIn: the square cut
        lm = post.get("linkedin_media")
        lv = Q / slot / lm if lm else None
        if not lm: fails.append("reel needs linkedin_media (the 1080x1080 cut for LinkedIn, reels build/build_square.sh)")
        elif not lv.exists(): fails.append(f"linkedin_media missing: {lm}")
        else:
            pr, pm = media_probe(lv), media_probe(Q / slot / media[0]["path"])
            if pr and pr[:2] != (1080, 1080): fails.append(f"{lm}: {pr[0]}x{pr[1]}, LinkedIn cut must be 1080x1080")
            if pr and pm and abs(pr[2] - pm[2]) > 1.0: fails.append(f"{lm} is {pr[2]:.1f} s, the reel is {pm[2]:.1f} s: rebuild the square cut from this reel")
            if lb and post.get("logo_bug"):
                sc = logo_bug_scores(lv, AP / lb["template"], lb["widths_px"], lb["end_ident_s"])
                if min(sc) < lb["min_match"]: fails.append(f"{lm}: logo bug not found in every sampled frame (scores {sc})")
            slc = CFG.get("sound_logo")
            if slc and (post.get("pillar") == "sale" or post.get("end_ident")) and sting_match(lv, (AP / slc["full"]).resolve()) < slc.get("min_match", 0.5):
                fails.append(f"{lm}: the Laqta snap is not audible at the end")
    sl = CFG.get("sound_logo")
    if t == "reel" and sl and (post.get("pillar") == "sale" or post.get("end_ident")):   # sales/customer videos end with the final ident (optional elsewhere)
        if (post.get("audio") or {}).get("end_sting") != sl["id"]:
            fails.append(f"reel must end with the sound logo: set audio.end_sting = '{sl['id']}' and mix it on the end logo")
        elif post["media_status"] == "ready" and media and (Q / slot / media[0]["path"]).exists():
            sw = (AP / sl["full"]).resolve()
            if not sw.exists(): fails.append(f"sound logo file missing: {sw}")
            else:
                m = sting_match(Q / slot / media[0]["path"], sw)
                if m < sl.get("min_match", 0.5): fails.append(f"the Laqta snap is not audible at the end of the reel (match {m:.2f} < {sl.get('min_match', 0.5)})")
    if t == "carousel" and post["media_status"] == "ready" and not 2 <= len(media) <= 10:
        fails.append(f"carousel needs 2-10 images, has {len(media)}")
    if t == "reel" and len(media) != 1: fails.append("a reel needs exactly one video")

    en, ar = post["caption_en"], post["caption_ar"]
    has_render = any(m.get("kind") in CFG["media_kinds_needing_label"] for m in media)
    if has_render:
        if not any(w in en.lower() for w in CFG["visualisation_caption_en"]):
            fails.append("renders/drawings in media: English caption must say so (e.g. '3D visualisation of the design')")
        if not any(w in ar for w in CFG["visualisation_caption_ar"]):
            fails.append("renders/drawings in media: Arabic caption must say so (e.g. «تصور ثلاثي الأبعاد للتصميم»)")
        if "تصوير ثلاثي" in ar: fails.append("use «تصور ثلاثي الأبعاد», not «تصوير» (STRATEGY.md claims rule 1)")

    # copy rules
    tags = post["hashtags"]
    if len(tags) > CFG["max_hashtags"]: fails.append(f"{len(tags)} hashtags, max {CFG['max_hashtags']}")
    if any(not h.startswith("#") or " " in h for h in tags): fails.append(f"bad hashtag in {tags}")
    allcopy = "\n".join([en, ar, " ".join(tags), post.get("on_screen_text", ""), post.get("yt_title", ""), post.get("li_doc_title", "")])
    if t == "reel":                                   # every Reel also goes to YouTube Shorts
        yt = post.get("yt_title", "")
        if not yt: fails.append("reel needs yt_title (YouTube Shorts title, English, <= 60 characters)")
        elif len(yt) > CFG.get("youtube_title_max", 60): fails.append(f"yt_title is {len(yt)} characters, max {CFG.get('youtube_title_max', 60)}")
    if t == "carousel" and CFG["buffer_channels"].get("linkedin"):   # carousels also go to the LinkedIn page as a document (PDF)
        lt = post.get("li_doc_title", "")
        if not lt: fails.append("carousel needs li_doc_title (LinkedIn document title, English, <= 60 characters)")
        elif len(lt) > CFG.get("linkedin_doc_title_max", 60): fails.append(f"li_doc_title is {len(lt)} characters, max {CFG.get('linkedin_doc_title_max', 60)}")
        if post["media_status"] == "ready":
            from common import LI_PDF
            pdf = Q / slot / LI_PDF
            srcs = [Q / slot / m["path"] for m in media]
            if not pdf.exists(): fails.append(f"{LI_PDF} missing: run tools/li_document.py {slot}")
            elif json.loads((pdf.with_suffix(".json")).read_text() if pdf.with_suffix(".json").exists() else "{}").get("sources") != [hashlib.sha1(x.read_bytes()).hexdigest() for x in srcs if x.exists()]:
                fails.append(f"a slide changed after {LI_PDF} was built: run tools/li_document.py {slot}")
            else:
                n = len(re.findall(rb"/Type\s*/Page(?!s)", pdf.read_bytes()))
                if n != len(media): fails.append(f"{LI_PDF} has {n} pages, the carousel has {len(media)} slides")
    sj = Q / slot / "slides.json"                     # on-screen text of rendered slides/Stories is copy too
    if sj.exists(): allcopy += "\n" + sj.read_text()
    for w in CFG["banned_words"]:
        if re.search(r"(?<![\w])" + re.escape(w) + r"(?![\w])", allcopy, re.I): fails.append(f"banned word: '{w}'")
    for pat in CFG.get("banned_patterns", []):
        m = re.search(pat, allcopy, re.I)
        if m: fails.append(f"exact spec or model name '{m.group(0)}' (keep specs vague: STRATEGY.md copy style)")
    if post.get("pillar") == "sale":                  # we sell the booth: no "we design the frame in your brand" (any variant)
        from common import youtube_captions, linkedin_text
        sale_copy = allcopy + "\n" + "\n".join(youtube_captions(post)) + "\n" + linkedin_text(post)
        for pat in CFG.get("sale_banned_patterns", {}).get("patterns", []):
            m = re.search(pat, sale_copy, re.I)
            if m: fails.append(f"sale post offers frame/brand design: '{m.group(0)}' (Hussain 24 Nov: we sell the booth)")
    if post.get("pillar") == "sale":                  # Hussain 24 Nov: always mention the professional lighting
        need = CFG.get("sale_must_mention", {})
        if not any(w in en.lower() for w in need.get("en", [])) and need.get("en"): fails.append("sale post: English must mention 'professional lighting'")
        if not any(w in ar for w in need.get("ar", [])) and need.get("ar"): fails.append("sale post: Arabic must mention «إضاءة احترافية»")
    for w in CFG["banned_claims"]:
        if w.lower() in allcopy.lower(): fails.append(f"banned claim: '{w}' (the booth seats two; no AI guests)")
    if not post.get("venue_confirmed_in_writing"):
        for v in CFG["venues_need_written_confirmation"]:
            if v.lower() in allcopy.lower(): fails.append(f"names venue '{v}' without venue_confirmed_in_writing: true")
    from common import compose_caption
    if len(compose_caption(post)) > CFG["caption_max_chars"]: fails.append("caption over 2,200 characters")

    hz = post.get("humanizer") or {}
    if not hz.get("checked"):
        fails.append("English caption not humanizer-checked (run the humanizer, then scripts/stamp.py humanizer <slot> <name>)")
    elif hz.get("caption_sha") != sha(en + post.get("yt_title", "") + post.get("li_doc_title", "")):
        fails.append("English caption changed after the humanizer check: run it again and re-stamp")

    ok, detail = arabic_gate(slot, ar, run_checker)
    if not ok: fails.append(f"Arabic caption did not pass arabic-newsroom: {detail}")
    if t in ("reel", "carousel"):                     # the YouTube/LinkedIn Arabic (WhatsApp line) must pass too
        from common import youtube_captions, linkedin_text
        yen, yar = youtube_captions(post)
        ok, detail = arabic_gate(slot, yar, run_checker, stamp_name="ar_gate_youtube.json")
        if not ok: fails.append(f"YouTube Arabic description did not pass arabic-newsroom: {detail}")
        for w in CFG["banned_words"]:
            if re.search(r"(?<![\w])" + re.escape(w) + r"(?![\w])", yen + "\n" + yar, re.I): fails.append(f"banned word in YouTube/LinkedIn copy: '{w}'")
        li = linkedin_text(post)
        if len(re.findall(r"(?<!\w)#[^\W\d_]", li)) > CFG.get("linkedin_max_hashtags", 3): fails.append("LinkedIn text has more than 3 hashtags")
        if re.search(r"\bDM us\b", li): fails.append("LinkedIn text still says 'DM us' (use the WhatsApp / website line)")

    if CFG["tagline_en"].lower() not in en.lower() and CFG["tagline_ar"] not in ar and post.get("pillar") != "sale":
        warns.append("no tagline (Make it a Laqta / خلّها لقطة)")
    for pat in AI_TELLS:
        if re.search(pat, en, re.I): warns.append(f"possible AI tell in English: /{pat}/")
    if post.get("publish_via") == "manual":
        warns.append("publish_via=manual: post this one by hand (stickers/music force Buffer's notify mode)")
    return fails, warns


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    run_checker = "--no-arabic-run" not in sys.argv
    targets = slots() if "--all" in sys.argv or not args else args
    bad = pending = 0
    for s in targets:
        fails, warns = check(s, run_checker)
        ready = load_post(s).get("media_status") == "ready"
        d = Q / s
        for f in ("gates_passed", "gates_failed"): (d / f).unlink(missing_ok=True)
        if fails and not ready and "--all" in sys.argv:          # media still to shoot/make: listed, not a CI failure
            pending += 1
            (d / "gates_failed").write_text("\n".join(fails) + "\n")
            print(f"PENDING {s} (media not ready)"); [print("   x", f) for f in fails]
        elif fails:
            bad += 1
            (d / "gates_failed").write_text("\n".join(fails) + "\n")
            print(f"FAIL {s}"); [print("   x", f) for f in fails]
        else:
            (d / "gates_passed").write_text(sha((d / "post.json").read_text()) + "\n")
            print(f"PASS {s}")
        [print("   !", w) for w in warns]
    print(f"\n{len(targets) - bad - pending}/{len(targets)} slots pass" + (f", {pending} pending media" if pending else ""))
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
