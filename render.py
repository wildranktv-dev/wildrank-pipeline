#!/usr/bin/env python3
"""Wild Rank renderer.

Reads a job JSON (jobs/<slug>.json) and produces in out/<slug>/:
  - <slug>.mp4            long-form 1920x1080 video
  - <slug>_thumb.jpg      1280x720 thumbnail
  - <slug>_short_<n>.mp4  vertical 1080x1920 Shorts (one per item listed in job["shorts"])
  - manifest.json         durations, footage credits, file list

Needs env: ELEVENLABS_API_KEY, PIXABAY_API_KEY. Needs ffmpeg/ffprobe on PATH.
"""
import json
import os
import random
import subprocess
import sys
import time
from pathlib import Path

import requests
from PIL import Image, ImageDraw, ImageEnhance, ImageFont

W, H, FPS = 1920, 1080, 30
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
DEFAULT_VOICE = "JBFqnCBsd6RMkjVDRZzb"  # ElevenLabs premade narrator voice
DENY_TAGS = {"3d", "animation", "cartoon", "render", "cgi", "ai generated", "illustration", "anime", "toy"}
EL_KEY = os.environ["ELEVENLABS_API_KEY"]
PX_KEY = os.environ["PIXABAY_API_KEY"]
SLUG = None
JOB = {}


def run(cmd):
    print("+", " ".join(str(c) for c in cmd)[:300], flush=True)
    subprocess.run(cmd, check=True)


def duration(path):
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
        check=True, capture_output=True, text=True).stdout.strip()
    return float(out)


def request_retry(method, url, tries=4, **kw):
    for i in range(tries):
        r = requests.request(method, url, timeout=120, **kw)
        if r.status_code < 500 and r.status_code != 429:
            return r
        time.sleep(2 ** i * 3)
    return r


# ---------- voice ----------
def pick_voice(job):
    vid = job.get("voice_id") or DEFAULT_VOICE
    r = request_retry("GET", "https://api.elevenlabs.io/v1/voices", headers={"xi-api-key": EL_KEY})
    if r.ok:
        ids = [v["voice_id"] for v in r.json().get("voices", [])]
        if ids and vid not in ids:
            print(f"voice {vid} not available, using {ids[0]}")
            vid = ids[0]
    return vid


REPO_RELEASES = "https://github.com/wildranktv-dev/wildrank-pipeline/releases/download"


def tts(text, voice, path, slug=None):
    """Generate narration, reusing audio already stored in the job's release (saves credits on re-renders)."""
    import hashlib
    if path.exists():
        return
    key = hashlib.md5(f"{voice}|{text}".encode()).hexdigest()[:16]
    cached = path.with_name(f"tts_{key}.mp3")
    if slug:
        r = requests.get(f"{REPO_RELEASES}/{slug}/{cached.name}", timeout=60)
        if r.ok and len(r.content) > 1000:
            path.write_bytes(r.content)
            cached.write_bytes(r.content)
            print(f"reused narration {cached.name}")
            return
    r = request_retry(
        "POST",
        f"https://api.elevenlabs.io/v1/text-to-speech/{voice}?output_format=mp3_44100_128",
        headers={"xi-api-key": EL_KEY, "Content-Type": "application/json"},
        json={"text": text, "model_id": "eleven_multilingual_v2",
              "voice_settings": {"stability": 0.45, "similarity_boost": 0.8, "style": 0.25}})
    if not r.ok:
        raise RuntimeError(f"ElevenLabs error {r.status_code}: {r.text[:300]}")
    path.write_bytes(r.content)
    cached.write_bytes(r.content)


# ---------- footage ----------
used_ids = set()
credits = []


def search_clips(query, must=None, n=8):
    r = request_retry("GET", "https://pixabay.com/api/videos/", params={
        "key": PX_KEY, "q": query, "per_page": 30, "safesearch": "true", "order": "popular"})
    r.raise_for_status()
    hits = []
    for h in r.json().get("hits", []):
        if (h["id"] in used_ids and not JOB.get("allow_reuse")) or h["id"] in JOB.get("deny_ids", []):
            continue
        tags = [t.strip().lower() for t in h.get("tags", "").split(",") if t.strip()]
        if any(t in DENY_TAGS or t in JOB.get("deny", []) for t in tags):
            continue
        # the subject must be one of the first 3 tags, matched exactly
        if must and not any(t in must for t in tags[:3]):
            continue
        v = h["videos"].get("large") or {}
        if not v.get("url") or v.get("width", 0) < 1280:
            v = h["videos"].get("medium") or {}
        if not v.get("url") or v.get("width", 0) < v.get("height", 1):
            continue
        hits.append((h, v))
    return hits[:n]


used_photos = set()


def search_photos(query, must=None, n=12, allow_used=False):
    r = request_retry("GET", "https://pixabay.com/api/", params={
        "key": PX_KEY, "q": query, "image_type": "photo", "orientation": "horizontal",
        "per_page": 50, "safesearch": "true", "order": "popular", "min_width": 1600})
    if not r.ok:
        return []
    hits = []
    for h in r.json().get("hits", []):
        if (h["id"] in used_photos and not allow_used) or h["id"] in JOB.get("deny_ids", []):
            continue
        tags = [t.strip().lower() for t in h.get("tags", "").split(",") if t.strip()]
        if any(t in DENY_TAGS or t in JOB.get("deny", []) for t in tags):
            continue
        if must and not any(t in must for t in tags[:3]):
            continue
        hits.append(h)
    return hits[:n]


def get_photos(seg, workdir, count):
    out = []
    queries = seg.get("photo_queries", seg["queries"]) + JOB.get("photo_queries", [])
    seen = set()
    for allow_used in (False, True):
      for q in queries:
        for h in search_photos(q, seg.get("must") or JOB.get("pool_must"), allow_used=allow_used):
            if len(out) >= count:
                return out
            if h["id"] in seen:
                continue
            seen.add(h["id"])
            p = workdir / f"img_{h['id']}.jpg"
            if not p.exists():
                rr = request_retry("GET", h.get("largeImageURL") or h["webformatURL"])
                if not rr.ok:
                    continue
                p.write_bytes(rr.content)
            used_photos.add(h["id"])
            credits.append({"id": h["id"], "type": "photo", "user": h.get("user"), "page": h.get("pageURL"),
                            "query": q, "tags": h.get("tags")})
            out.append((p, 4.5))
    return out


def _get_video_clips(seg, workdir, need_seconds):
    """Return (clips, illustrative). Specific queries must match `must` tags; otherwise use fallback queries."""
    plans = [(q, seg.get("must")) for q in seg["queries"]]
    plans += [(q, JOB.get("pool_must")) for q in JOB.get("pool_queries", []) if q not in seg["queries"]]
    fallback = [(q, seg.get("fallback_must")) for q in seg.get("fallback", [])]
    for group, illustrative in ((plans, False), (fallback, True)):
        files, total = [], 0.0
        for q, must in group:
            for h, v in search_clips(q, must):
                p = workdir / f"px_{h['id']}.mp4"
                if not p.exists():
                    rr = request_retry("GET", v["url"])
                    if not rr.ok:
                        continue
                    p.write_bytes(rr.content)
                used_ids.add(h["id"])
                credits.append({"id": h["id"], "user": h.get("user"), "page": h.get("pageURL"), "query": q,
                                "tags": h.get("tags")})
                d = min(duration(p), 6.0)
                files.append((p, d))
                total += d
                if total >= need_seconds:
                    return files, illustrative
        if len(files) >= 2:
            return files, illustrative
    raise RuntimeError(f"no footage for {seg.get('name') or seg.get('overlay')}")


def get_clips(seg, workdir, need_seconds):
    if not JOB.get("photos"):
        return _get_video_clips(seg, workdir, need_seconds)
    vids, illus = _get_video_clips(seg, workdir, need_seconds * 0.45)
    photos = get_photos(seg, workdir, int(need_seconds * 0.6 / 4.5) + 1)
    mixed = []
    for i in range(max(len(vids), len(photos))):
        if i < len(photos):
            mixed.append(photos[i])
        if i < len(vids):
            mixed.append(vids[i])
    return mixed, illus


# ---------- video building ----------
def esc(s):
    return s.replace("\\", "\\\\").replace(":", "\\:").replace("'", "’").replace("%", "\\%")


def build_segment(seg, idx, voice, work):
    audio = work / f"a{idx:02d}.mp3"
    tts(seg["text"], voice, audio, SLUG)
    adur = duration(audio)
    vdur = adur + 0.6
    clips, illustrative = get_clips(seg, work, vdur)

    # normalise clips and loop through them until vdur is covered
    parts, t, k = [], 0.0, 0
    while t < vdur:
        src, d = clips[k % len(clips)]
        d = min(d, vdur - t)
        if d < 0.5 and parts:
            break
        out = work / f"s{idx:02d}_{k:02d}.mp4"
        if src.suffix == ".jpg":
            frames = int(d * FPS) + 1
            zin = k % 2 == 0
            z = "min(1.0+0.0009*on,1.15)" if zin else "max(1.15-0.0009*on,1.0)"
            run(["ffmpeg", "-y", "-loglevel", "error", "-loop", "1", "-i", str(src), "-t", f"{d:.3f}",
                 "-vf", f"scale=3840:2160:force_original_aspect_ratio=increase,crop=3840:2160,"
                        f"zoompan=z='{z}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d={frames}:s={W}x{H}:fps={FPS},setsar=1",
                 "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p", str(out)])
            parts.append(out)
            t += d
            k += 1
            continue
        start = 0.5 if duration(src) > d + 1 else 0
        run(["ffmpeg", "-y", "-loglevel", "error", "-ss", f"{start}", "-t", f"{d:.3f}", "-i", str(src),
             "-vf", f"scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},fps={FPS},setsar=1",
             "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p", str(out)])
        parts.append(out)
        t += d
        k += 1

    lst = work / f"s{idx:02d}.txt"
    lst.write_text("".join(f"file '{p.name}'\n" for p in parts))
    raw = work / f"s{idx:02d}_raw.mp4"
    run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy", str(raw)])

    # text overlays
    filters = []
    card_png = None
    if seg.get("specs"):
        card_png = work / f"card{idx:02d}.png"
        make_card(seg, card_png)
        filters.append(
            f"drawtext=fontfile={FONT}:text='#{seg['rank']}':fontsize=320:fontcolor=white:"
            f"borderw=10:bordercolor=black@0.7:x=(w-tw)/2:y=(h-th)/2:enable='lt(t,2.0)'")
    elif seg.get("rank") is not None:
        label = esc(f"#{seg['rank']}  {seg['name'].upper()}")
        filters.append(
            f"drawtext=fontfile={FONT}:text='{label}':fontsize=64:fontcolor=white:"
            f"box=1:boxcolor=black@0.55:boxborderw=24:x=60:y=h-th-90")
        filters.append(
            f"drawtext=fontfile={FONT}:text='#{seg['rank']}':fontsize=320:fontcolor=white:"
            f"borderw=10:bordercolor=black@0.7:x=(w-tw)/2:y=(h-th)/2:enable='lt(t,2.2)'")
        if seg.get("stat"):
            filters.append(
                f"drawtext=fontfile={FONT}:text='{esc(seg['stat'])}':fontsize=52:fontcolor=#FFD23F:"
                f"box=1:boxcolor=black@0.55:boxborderw=18:x=60:y=60:enable='between(t,2.5,{max(3.0, adur - 1):.2f})'")
    elif seg.get("overlay"):
        filters.append(
            f"drawtext=fontfile={FONT}:text='{esc(seg['overlay'])}':fontsize=96:fontcolor=white:"
            f"borderw=8:bordercolor=black@0.7:x=(w-tw)/2:y=(h-th)/2:enable='lt(t,4)'")
    if illustrative or JOB.get("always_illustrative"):
        filters.append(f"drawtext=fontfile={FONT}:text='{esc(JOB.get('illustrative_label', 'Illustrative footage'))}'"
                       f":fontsize=28:fontcolor=white@0.85:box=1:boxcolor=black@0.4:boxborderw=10:x=w-tw-40:y=h-th-40")
    filters.append(f"drawtext=fontfile={FONT}:text='{esc(JOB.get('brand', 'WILD RANK'))}'"
                   f":fontsize=34:fontcolor=white@0.8:x=w-tw-40:y=36")
    seg_out = work / f"seg{idx:02d}.mp4"
    if card_png:
        fc = (f"[0:v]{','.join(filters)}[b];[2:v]format=rgba[c];"
              f"[b][c]overlay=0:0:enable='gte(t,2.0)'[v];[1:a]adelay=200|200,apad[a]")
        inputs = ["-i", str(raw), "-i", str(audio), "-loop", "1", "-i", str(card_png)]
    else:
        fc = f"[0:v]{','.join(filters)}[v];[1:a]adelay=200|200,apad[a]"
        inputs = ["-i", str(raw), "-i", str(audio)]
    run(["ffmpeg", "-y", "-loglevel", "error", *inputs,
         "-filter_complex", fc,
         "-map", "[v]", "-map", "[a]", "-t", f"{vdur:.3f}",
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-b:a", "192k", "-ar", "44100", str(seg_out)])
    return seg_out, raw, illustrative, adur


# ---------- subtitles ----------
def _chunks(text, maxlen=84):
    import re
    sents = re.split(r"(?<=[.!?])\s+", text.strip())
    out = []
    for snt in sents:
        words, cur = snt.split(), ""
        for w in words:
            if len(cur) + len(w) + 1 > maxlen and cur:
                out.append(cur)
                cur = w
            else:
                cur = f"{cur} {w}".strip()
        if cur:
            out.append(cur)
    return out


def _ts(t):
    h, rem = divmod(t, 3600)
    m, sec = divmod(rem, 60)
    return f"{int(h):02d}:{int(m):02d}:{int(sec):02d},{int(round((sec % 1) * 1000)) % 1000:03d}"


def write_srt(segments, timings, field, path):
    """timings: list of (start, audio_duration) per segment. Text chunks are spread by length over the narration."""
    lines, n = [], 1
    for seg, (start, adur) in zip(segments, timings):
        text = seg.get(field)
        if not text:
            continue
        parts = _chunks(text)
        total = sum(len(p) for p in parts) or 1
        t = start + 0.2
        for p in parts:
            d = adur * len(p) / total
            if len(p) > 42:
                cut = p.rfind(" ", 0, len(p) // 2 + 6)
                p = p[:cut] + "\n" + p[cut + 1:]
            lines.append(f"{n}\n{_ts(t)} --> {_ts(t + d - 0.05)}\n{p}\n")
            n += 1
            t += d
    path.write_text("\n".join(lines), encoding="utf-8")


CARD_BG = (12, 14, 30, 225)
CY, MG = (0, 214, 255), (170, 80, 255)


def make_card(seg, path, accent=CY):
    """Transparent 1920x1080 PNG with a product spec panel on the left (Setup Rank)."""
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    x0, y0, x1 = 60, 150, 980
    lines = seg.get("specs", [])
    f_rank = ImageFont.truetype(FONT, 64)
    f_name = ImageFont.truetype(FONT, 50 if len(seg["name"]) <= 24 else 40)
    f_tag = ImageFont.truetype(FONT, 30)
    f_spec = ImageFont.truetype(FONT, 34)
    for_lines = []
    if seg.get("for"):
        cur = ""
        for w in ("Para quem: " + seg["for"]).split():
            test = f"{cur} {w}".strip()
            if d.textlength(test, font=f_tag) > (x1 - x0 - 72) and cur:
                for_lines.append(cur)
                cur = w
            else:
                cur = test
        for_lines.append(cur)
    h = 250 + 62 * len(lines) + 42 * len(for_lines) + (30 if for_lines else 0)
    d.rounded_rectangle([x0, y0, x1, y0 + h], radius=28, fill=CARD_BG, outline=accent, width=4)
    d.text((x0 + 36, y0 + 28), f"#{seg['rank']}", font=f_rank, fill=accent)
    if seg.get("tier"):
        tw = d.textlength(seg["tier"], font=f_tag)
        d.rounded_rectangle([x1 - tw - 70, y0 + 40, x1 - 30, y0 + 88], radius=20, fill=MG)
        d.text((x1 - tw - 50, y0 + 46), seg["tier"], font=f_tag, fill=(255, 255, 255))
    d.text((x0 + 36, y0 + 118), seg["name"], font=f_name, fill=(255, 255, 255))
    y = y0 + 205
    for ln in lines:
        d.ellipse([x0 + 40, y + 12, x0 + 54, y + 26], fill=accent)
        d.text((x0 + 72, y), ln, font=f_spec, fill=(230, 235, 245))
        y += 62
    y += 14
    for fl in for_lines:
        d.text((x0 + 36, y), fl, font=f_tag, fill=accent)
        y += 42
    img.save(path)


def make_short(seg_file, raw_file, seg, out, illustrative=False):
    d = min(duration(seg_file), 58.0)
    label = f"#{seg['rank']} {seg['name'].upper()}"
    fs = 78 if len(label) <= 16 else max(44, int(78 * 16 / len(label)))
    top = esc(label)
    vf = (f"crop=ih*9/16:ih,scale=1080:1920,setsar=1,"
          f"drawtext=fontfile={FONT}:text='{top}':fontsize={fs}:fontcolor=white:box=1:boxcolor=black@0.6:"
          f"boxborderw=26:x=(w-tw)/2:y=220,"
          f"drawtext=fontfile={FONT}:text='{esc(JOB.get('short_footer', 'Full ranking on Wild Rank'))}'"
          f":fontsize=46:fontcolor=#FFD23F:box=1:boxcolor=black@0.6:boxborderw=18:x=(w-tw)/2:y=h-360")
    if illustrative or JOB.get("always_illustrative"):
        vf += (f",drawtext=fontfile={FONT}:text='{esc(JOB.get('illustrative_label', 'Illustrative footage'))}'"
               f":fontsize=30:fontcolor=white@0.85:"
               f"box=1:boxcolor=black@0.4:boxborderw=10:x=(w-tw)/2:y=h-260")
    card = Path(raw_file).with_name("card" + Path(raw_file).name[1:].replace("_raw.mp4", ".png"))
    if seg.get("specs") and card.exists():
        im = Image.open(card)
        im = im.crop(im.getbbox())
        im = im.resize((1000, int(im.height * 1000 / im.width)))
        cs = card.with_name(card.stem + "_short.png")
        im.save(cs)
        fc = f"[0:v]{vf}[b];[2:v]format=rgba[c];[b][c]overlay=(W-w)/2:H-h-470:enable='gte(t,2.0)'[v]"
        run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(raw_file), "-i", str(seg_file), "-loop", "1",
             "-i", str(cs), "-t", f"{d:.3f}", "-filter_complex", fc, "-map", "[v]", "-map", "1:a",
             "-c:v", "libx264", "-preset", "veryfast", "-crf", "21", "-pix_fmt", "yuv420p",
             "-c:a", "aac", "-b:a", "160k", str(out)])
        return
    run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(raw_file), "-i", str(seg_file), "-t", f"{d:.3f}",
         "-map", "0:v", "-map", "1:a", "-vf", vf,
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "21", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-b:a", "160k", str(out)])


def make_thumb(src_video, text_lines, out):
    frame = out.with_suffix(".frame.jpg")
    run(["ffmpeg", "-y", "-loglevel", "error", "-ss", "3", "-i", str(src_video), "-frames:v", "1", str(frame)])
    img = Image.open(frame).convert("RGB").resize((1280, 720))
    img = ImageEnhance.Brightness(img).enhance(0.75)
    img = ImageEnhance.Contrast(img).enhance(1.25)
    d = ImageDraw.Draw(img)
    y = 70
    for i, line in enumerate(text_lines):
        size = 120 if i == 0 else 84
        f = ImageFont.truetype(FONT, size)
        color = (255, 210, 63) if i == 0 else (255, 255, 255)
        d.text((60, y), line, font=f, fill=color, stroke_width=8, stroke_fill=(0, 0, 0))
        y += size + 24
    img.save(out, quality=92)
    frame.unlink(missing_ok=True)


def main(job_path):
    job = json.loads(Path(job_path).read_text())
    global SLUG, JOB
    JOB = job
    slug = job["slug"]
    SLUG = slug
    out_dir = Path("out") / slug
    work = Path("work") / slug
    out_dir.mkdir(parents=True, exist_ok=True)
    work.mkdir(parents=True, exist_ok=True)
    voice = pick_voice(job)

    segments = job["segments"]
    seg_files, raw_files, illus_flags, audio_durs = [], [], [], []
    for i, seg in enumerate(segments):
        out, raw, illus, adur = build_segment(seg, i, voice, work)
        audio_durs.append(adur)
        illus_flags.append(illus)
        seg_files.append(out)
        raw_files.append(raw)

    lst = work / "final.txt"
    lst.write_text("".join(f"file '{p.name}'\n" for p in seg_files))
    final = out_dir / f"{slug}.mp4"
    run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0", "-i", str(lst),
         "-c:v", "libx264", "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", str(final)])

    files = [final.name]
    timings, t0 = [], 0.0
    for sf, ad in zip(seg_files, audio_durs):
        timings.append((t0, ad))
        t0 += duration(sf)
    for field, lang in (("text", job.get("lang", "en")), ("text_pt", "pt")):
        if any(sg.get(field) for sg in segments):
            sp = out_dir / f"{slug}_{lang}.srt"
            write_srt(segments, timings, field, sp)
            files.append(sp.name)
    for n, rank in enumerate(job.get("shorts", []), 1):
        i = next(k for k, s in enumerate(segments) if s.get("rank") == rank)
        sp = out_dir / f"{slug}_short_{n}.mp4"
        make_short(seg_files[i], raw_files[i], segments[i], sp, illus_flags[i])
        files.append(sp.name)

    thumb_src = raw_files[next(k for k, s in enumerate(segments) if s.get("rank") == job.get("thumb_rank", 1))]
    thumb = out_dir / f"{slug}_thumb.jpg"
    make_thumb(thumb_src, job["thumbnail_text"], thumb)
    files.append(thumb.name)

    for f in work.glob("tts_*.mp3"):
        (out_dir / f.name).write_bytes(f.read_bytes())
    manifest = {"slug": slug, "duration": duration(final), "files": files, "credits": credits,
                "chars": sum(len(s["text"]) for s in segments)}
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2))
    print(json.dumps(manifest, indent=2))


if __name__ == "__main__":
    main(sys.argv[1])
