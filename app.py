"""
app.py — Railway version, adapted from extract_server.py v4
- /health: health check
- /search: metadata-only search by song/video name (no download)
- /extract: runs yt-dlp, returns a stream_url instead of the file itself
- /media/<job_id>: serves the extracted file with HTTP range support
- /info: fast metadata-only lookup for a direct URL
- Background cleanup of job folders older than 1 hour
"""

import os
import json
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
import urllib.parse
import re
from flask import Flask, request, jsonify, send_file
from pedalboard import Pedalboard, Gain, Limiter, PeakFilter
from pedalboard.io import AudioFile

app = Flask(__name__)

PORT = int(os.environ.get("PORT", 8000))
SHARED_SECRET = os.environ.get("EXTRACT_SHARED_SECRET", "change-me")
MAX_AGE_SECONDS = 3600
CLEANUP_INTERVAL = 600

AUDIO_QUALITY_MAP = {"low": "128", "medium": "192", "high": "256", "max": "320"}
VIDEO_QUALITY_MAP = {"low": 480, "medium": 720, "high": 1080, "max": 2160}

JOBS_ROOT = tempfile.gettempdir()

EXTRACT_SLOTS = threading.Semaphore(3)
SEARCH_SLOTS = threading.Semaphore(4)

# Cookies not set up yet - only used if this file happens to exist later
YTDLP_COOKIES = os.path.expanduser("~/ytcookies/cookies_fixed.txt")
YTDLP_POT_ARGS = []

# YouTube now requires running some of its player JS to derive stream
# signatures. yt-dlp needs a JS runtime for this - Deno is installed via
# the Dockerfile. Without this flag yt-dlp still tries to auto-detect one,
# but being explicit avoids "No supported JavaScript runtime could be
# found" if detection ever fails silently.
JS_RUNTIME_ARGS = ["--extractor-args", "youtube:jsruntime=deno"]


def cookie_args():
    return ["--cookies", YTDLP_COOKIES] if os.path.exists(YTDLP_COOKIES) else []


def enhance_audio(input_path, output_path):
    board = Pedalboard([
        PeakFilter(cutoff_frequency_hz=2500, gain_db=3, q=0.7),
        Gain(gain_db=6),
        Limiter(threshold_db=-1.0, release_ms=100),
    ])
    with AudioFile(input_path) as f:
        audio = f.read(f.frames)
        samplerate = f.samplerate
    effected = board(audio, samplerate)
    with AudioFile(output_path, "w", samplerate, effected.shape[0]) as f:
        f.write(effected)


def enhance_video(input_path, output_path, target_height=2160):
    target_width = int(target_height * 16 / 9)
    vf = (
        f"scale={target_width}:{target_height}:flags=lanczos,"
        f"unsharp=5:5:1.0:5:5:0.0"
    )
    cmd = ["ffmpeg", "-y", "-i", input_path, "-vf", vf, "-c:a", "copy", output_path]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if result.returncode != 0:
        raise RuntimeError(f"video enhance failed: {result.stderr[-1000:]}")


def check_auth(req):
    return req.headers.get("X-Auth-Token") == SHARED_SECRET


def cleanup_old_jobs():
    while True:
        try:
            now = time.time()
            for name in os.listdir(JOBS_ROOT):
                if not name.startswith("extract_"):
                    continue
                full_path = os.path.join(JOBS_ROOT, name)
                if not os.path.isdir(full_path):
                    continue
                age = now - os.path.getmtime(full_path)
                if age > MAX_AGE_SECONDS:
                    shutil.rmtree(full_path, ignore_errors=True)
                    print(f"[cleanup] removed stale job dir: {name} (age={int(age)}s)")
        except Exception as e:
            print(f"[cleanup] error during sweep: {e}")
        time.sleep(CLEANUP_INTERVAL)


# Starts on import so it also runs under gunicorn, not just `python app.py`
threading.Thread(target=cleanup_old_jobs, daemon=True).start()


@app.route("/health")
def health():
    return jsonify({"status": "ok"}), 200


@app.route("/search", methods=["POST"])
def search():
    if not check_auth(request):
        return jsonify({"error": "unauthorized"}), 401
    if not SEARCH_SLOTS.acquire(blocking=False):
        return jsonify({"error": "server busy, try again shortly"}), 429

    data = request.get_json(silent=True) or {}
    query = data.get("query")
    limit = min(int(data.get("limit", 5)), 10)

    if not query:
        SEARCH_SLOTS.release()
        return jsonify({"error": "missing 'query'"}), 400

    cmd = [
        "yt-dlp", f"ytsearch{limit}:{query}",
        *cookie_args(), *YTDLP_POT_ARGS, *JS_RUNTIME_ARGS,
        "--flat-playlist", "--dump-json", "--no-warnings",
    ]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired:
        SEARCH_SLOTS.release()
        return jsonify({"error": "search timed out"}), 504

    if result.returncode != 0:
        SEARCH_SLOTS.release()
        return jsonify({"error": "search failed", "stderr": result.stderr[-1000:]}), 502

    results = []
    for line in result.stdout.strip().splitlines():
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        results.append({
            "id": entry.get("id"),
            "title": entry.get("title"),
            "uploader": entry.get("uploader") or entry.get("channel"),
            "duration": entry.get("duration"),
            "thumbnail": entry.get("thumbnails", [{}])[-1].get("url") if entry.get("thumbnails") else None,
            "url": entry.get("url") or f"https://youtu.be/{entry.get('id')}",
        })

    SEARCH_SLOTS.release()
    return jsonify({"results": results})


@app.route("/extract", methods=["POST"])
def extract():
    if not check_auth(request):
        return jsonify({"error": "unauthorized"}), 401
    if not EXTRACT_SLOTS.acquire(blocking=False):
        return jsonify({"error": "server busy, try again shortly"}), 429

    data = request.get_json(silent=True) or {}
    url = data.get("url")
    mode = data.get("mode", "audio")
    quality = data.get("quality", "medium")

    if not url:
        EXTRACT_SLOTS.release()
        return jsonify({"error": "missing 'url'"}), 400
    if mode not in ("audio", "video"):
        EXTRACT_SLOTS.release()
        return jsonify({"error": "mode must be 'audio' or 'video'"}), 400

    job_id = uuid.uuid4().hex
    work_dir = os.path.join(JOBS_ROOT, f"extract_{job_id}")
    os.makedirs(work_dir, exist_ok=True)
    out_template = os.path.join(work_dir, "output.%(ext)s")
    info_json_path = os.path.join(work_dir, "output.info.json")

    if mode == "audio":
        bitrate = AUDIO_QUALITY_MAP.get(quality, "192")
        cmd = [
            "yt-dlp", "-x", "--audio-format", "mp3",
            *cookie_args(), *YTDLP_POT_ARGS, *JS_RUNTIME_ARGS,
            "--audio-quality", bitrate + "K",
            "--write-info-json", "-o", out_template, url,
        ]
        expected_path = os.path.join(work_dir, "output.mp3")
        mimetype = "audio/mpeg"
        download_ext = "mp3"
    else:
        height = VIDEO_QUALITY_MAP.get(quality, 720)
        fmt = f"bv*[height<={height}]+ba/b[height<={height}]/b"
        cmd = [
            "yt-dlp", "-f", fmt,
            *cookie_args(), *YTDLP_POT_ARGS, *JS_RUNTIME_ARGS,
            "--merge-output-format", "mp4",
            "--write-info-json", "-o", out_template, url,
        ]
        expected_path = os.path.join(work_dir, "output.mp4")
        mimetype = "video/mp4"
        download_ext = "mp4"

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    except subprocess.TimeoutExpired:
        EXTRACT_SLOTS.release()
        return jsonify({"error": "extraction timed out"}), 504

    if result.returncode != 0:
        EXTRACT_SLOTS.release()
        print(f"[extract] yt-dlp failed for mode={mode} url={url}: {result.stderr[-1000:]}")
        return jsonify({"error": "yt-dlp failed", "stderr": result.stderr[-2000:]}), 502

    if not os.path.exists(expected_path):
        EXTRACT_SLOTS.release()
        return jsonify({"error": f"extraction succeeded but {download_ext} not found"}), 500

    if data.get("enhance"):
        enhanced_path = os.path.join(work_dir, f"enhanced.{download_ext}")
        try:
            if mode == "audio":
                enhance_audio(expected_path, enhanced_path)
            else:
                enhance_video(expected_path, enhanced_path)
            os.replace(enhanced_path, expected_path)
        except Exception as e:
            print(f"[extract] enhance failed, serving original: {e}")

    title = "download"
    try:
        if os.path.exists(info_json_path):
            with open(info_json_path, "r") as f:
                title = json.load(f).get("title", "download")
    except Exception as e:
        print(f"[extract] could not read info.json: {e}")

    media_meta = {"mimetype": mimetype, "ext": download_ext, "title": title}
    with open(os.path.join(work_dir, "media.json"), "w") as f:
        json.dump(media_meta, f)

    host = request.host_url.rstrip("/").replace("http://", "https://")
    stream_url = f"{host}/media/{job_id}?token={urllib.parse.quote(SHARED_SECRET)}"

    EXTRACT_SLOTS.release()
    return jsonify({
        "job_id": job_id,
        "stream_url": stream_url,
        "title": title,
        "mimetype": mimetype,
        "ext": download_ext,
    })


@app.route("/media/<job_id>")
def media(job_id):
    token = request.args.get("token", "")
    if token != SHARED_SECRET:
        return jsonify({"error": "unauthorized"}), 401

    work_dir = os.path.join(JOBS_ROOT, f"extract_{job_id}")
    meta_path = os.path.join(work_dir, "media.json")
    if not os.path.exists(meta_path):
        return jsonify({"error": "job not found or expired"}), 404

    with open(meta_path) as f:
        meta = json.load(f)

    file_path = os.path.join(work_dir, f"output.{meta['ext']}")
    if not os.path.exists(file_path):
        return jsonify({"error": "media file missing"}), 404

    return send_file(file_path, mimetype=meta["mimetype"], conditional=True)


@app.route("/info", methods=["POST"])
def info():
    if not check_auth(request):
        return jsonify({"error": "unauthorized"}), 401

    data = request.get_json(silent=True) or {}
    url = data.get("url")
    if not url:
        return jsonify({"error": "missing 'url'"}), 400

    cmd = ["yt-dlp", "--dump-json", "--no-warnings", *cookie_args(), *YTDLP_POT_ARGS, *JS_RUNTIME_ARGS, "--skip-download", url]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
    except subprocess.TimeoutExpired:
        return jsonify({"error": "info lookup timed out"}), 504

    if result.returncode != 0:
        return jsonify({"error": "info lookup failed", "stderr": result.stderr[-1000:]}), 502

    try:
        info_data = json.loads(result.stdout.strip().splitlines()[0])
    except (json.JSONDecodeError, IndexError):
        return jsonify({"error": "could not parse yt-dlp output"}), 502

    return jsonify({
        "title": info_data.get("title"),
        "uploader": info_data.get("uploader") or info_data.get("channel"),
        "duration": info_data.get("duration"),
        "thumbnail": info_data.get("thumbnail"),
        "webpage_url": info_data.get("webpage_url", url),
        "extractor": info_data.get("extractor"),
    })



import requests

SPOTIFY_ID_RE = re.compile(r"playlist/([a-zA-Z0-9]+)")


def _deep_find_tracks(obj):
    """Fallback: recursively search the JSON for anything shaped like a track list,
    in case Spotify's structure differs from the known path (region/A-B testing)."""
    if isinstance(obj, dict):
        if "trackList" in obj and isinstance(obj["trackList"], list):
            return obj["trackList"]
        for v in obj.values():
            found = _deep_find_tracks(v)
            if found:
                return found
    elif isinstance(obj, list):
        for item in obj:
            found = _deep_find_tracks(item)
            if found:
                return found
    return None


@app.route("/spotify", methods=["POST"])
def spotify_playlist():
    if not check_auth(request):
        return jsonify({"error": "unauthorized"}), 401

    data = request.get_json(silent=True) or {}
    url = data.get("url")
    if not url:
        return jsonify({"error": "missing 'url'"}), 400

    match = SPOTIFY_ID_RE.search(url)
    if not match:
        return jsonify({"error": "could not extract playlist id from url"}), 400
    playlist_id = match.group(1)

    embed_url = f"https://open.spotify.com/embed/playlist/{playlist_id}"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"}

    try:
        resp = requests.get(embed_url, headers=headers, timeout=15)
    except requests.RequestException as e:
        return jsonify({"error": f"fetch failed: {e}"}), 502

    next_data_match = re.search(
        r'<script id="__NEXT_DATA__" type="application/json">(.+?)</script>',
        resp.text,
    )
    if not next_data_match:
        return jsonify({
            "error": "could not find __NEXT_DATA__ in embed page",
            "html_snippet": resp.text[:1000],
        }), 502

    try:
        next_data = json.loads(next_data_match.group(1))
    except json.JSONDecodeError:
        return jsonify({"error": "could not parse playlist json"}), 502

    entity = (
        next_data.get("props", {})
        .get("pageProps", {})
        .get("state", {})
        .get("data", {})
        .get("entity", {})
    )

    if not entity:
        return jsonify({
            "error": "unexpected embed page structure",
            "top_level_keys": list(next_data.keys()),
        }), 502

    tracks = entity.get("trackList") or _deep_find_tracks(next_data) or []

    results = []
    for t in tracks:
        results.append({
            "title": t.get("title"),
            "subtitle": t.get("subtitle"),
            "duration_ms": t.get("duration"),
            "uri": t.get("uri"),
        })

    return jsonify({
        "playlist_title": entity.get("title") or entity.get("name"),
        "cover_art": entity.get("coverArt", {}).get("sources", [{}])[-1].get("url") if entity.get("coverArt") else None,
        "track_count": len(results),
        "tracks": results,
    })



@app.route("/spotify/debug", methods=["POST"])
def spotify_debug():
    if not check_auth(request):
        return jsonify({"error": "unauthorized"}), 401

    data = request.get_json(silent=True) or {}
    url = data.get("url")
    if not url:
        return jsonify({"error": "missing 'url'"}), 400

    match = SPOTIFY_ID_RE.search(url)
    if not match:
        return jsonify({"error": "could not extract playlist id"}), 400
    playlist_id = match.group(1)

    embed_url = f"https://open.spotify.com/embed/playlist/{playlist_id}"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"}
    resp = requests.get(embed_url, headers=headers, timeout=15)

    next_data_match = re.search(
        r'<script id="__NEXT_DATA__" type="application/json">(.+?)</script>',
        resp.text,
    )
    if not next_data_match:
        return jsonify({"error": "no next_data found"}), 502

    next_data = json.loads(next_data_match.group(1))
    entity = (
        next_data.get("props", {})
        .get("pageProps", {})
        .get("state", {})
        .get("data", {})
        .get("entity", {})
    )

    # Return entity's top-level keys and any non-list values (skip dumping full trackList)
    summary = {}
    for k, v in entity.items():
        if isinstance(v, list):
            summary[k] = f"<list of {len(v)} items>"
        elif isinstance(v, dict):
            summary[k] = f"<dict with keys: {list(v.keys())}>"
        else:
            summary[k] = v

    return jsonify(summary)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=PORT, threaded=True)
