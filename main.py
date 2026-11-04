"""
OmniDownloader - Universal Video & Audio Downloader Web Application
-------------------------------------------------------------------
Full-stack web application powered by Flask, yt-dlp, and FFmpeg.
Supports YouTube, Instagram Reels/Posts, Facebook Watch/Reels,
TikTok, Twitter/X, Reddit, Vimeo, and 1000+ web platforms.

SETUP:
    pip install flask yt-dlp imageio-ffmpeg

RUN:
    python main.py
"""

import os
import re
import shutil
import threading
import time
import uuid
from flask import Flask, jsonify, render_template, request, send_file

try:
    import yt_dlp
except ImportError:
    yt_dlp = None

try:
    import imageio_ffmpeg
except ImportError:
    imageio_ffmpeg = None

# Base directories
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Locate template directory across local run and Vercel environments
template_dirs = [
    os.path.join(BASE_DIR, "templates"),
    os.path.join(BASE_DIR, "api", "templates"),
    os.path.join(os.path.dirname(BASE_DIR), "templates"),
    os.path.join(os.getcwd(), "templates"),
    os.path.join(os.getcwd(), "api", "templates"),
]
valid_template_dir = next((d for d in template_dirs if os.path.isdir(d)), os.path.join(BASE_DIR, "templates"))

app = Flask(__name__, template_folder=valid_template_dir)

# In-memory store for active download tasks
TASKS = {}


def get_cache_dir():
    """Lazily finds a writable directory for temporary downloads (safe for Vercel/Lambda)."""
    candidates = [
        os.path.join("/tmp", "downloads_cache"),
        os.path.join(BASE_DIR, "downloads_cache"),
    ]
    for p in candidates:
        try:
            os.makedirs(p, exist_ok=True)
            test_file = os.path.join(p, f".write_test_{uuid.uuid4().hex[:6]}")
            with open(test_file, "w") as f:
                f.write("ok")
            os.remove(test_file)
            return p
        except Exception:
            continue
    import tempfile
    return tempfile.gettempdir()


def get_cookie_file():
    """Checks for cookie file to bypass YouTube cloud bot detection."""
    cookie_paths = [
        os.path.join(BASE_DIR, "cookies.txt"),
        os.path.join(os.getcwd(), "cookies.txt"),
        os.path.join("/tmp", "cookies.txt"),
    ]
    for cp in cookie_paths:
        if os.path.isfile(cp):
            return cp
    # Check if cookies are passed in environment
    env_cookies = os.environ.get("YOUTUBE_COOKIES")
    if env_cookies:
        tmp_cookie = os.path.join(get_cache_dir(), "env_cookies.txt")
        try:
            with open(tmp_cookie, "w", encoding="utf-8") as f:
                f.write(env_cookies)
            return tmp_cookie
        except Exception:
            pass
    return None


def get_ffmpeg_path():
    """Finds ffmpeg executable from PATH or the imageio_ffmpeg package."""
    system_ffmpeg = shutil.which("ffmpeg")
    if system_ffmpeg:
        return system_ffmpeg
    if imageio_ffmpeg is not None:
        try:
            return imageio_ffmpeg.get_ffmpeg_exe()
        except Exception:
            pass
    return None


def clean_ansi(text):
    """Strips terminal ANSI color codes and clean error messages."""
    cleaned = re.sub(r"\x1b\[[0-9;]*[a-zA-Z]", "", str(text)).strip()
    return cleaned


def format_duration(seconds):
    """Formats seconds into mm:ss or hh:mm:ss."""
    if not seconds:
        return "--:--"
    try:
        seconds = int(seconds)
        hours = seconds // 3600
        minutes = (seconds % 3600) // 60
        secs = seconds % 60
        if hours > 0:
            return f"{hours}h {minutes:02d}m {secs:02d}s"
        return f"{minutes:02d}:{secs:02d}"
    except Exception:
        return "--:--"


def detect_platform(url, extractor_key=""):
    """Detects brand/platform from URL or extractor name."""
    url_lower = url.lower()
    ext_lower = str(extractor_key).lower()

    if "youtube" in url_lower or "youtu.be" in url_lower or "youtube" in ext_lower:
        return "YouTube"
    if "instagram" in url_lower or "instagr.am" in url_lower or "instagram" in ext_lower:
        return "Instagram"
    if "facebook" in url_lower or "fb.watch" in url_lower or "fb.com" in url_lower or "facebook" in ext_lower:
        return "Facebook"
    if "tiktok" in url_lower or "tiktok" in ext_lower:
        return "TikTok"
    if "twitter" in url_lower or "x.com" in url_lower or "twitter" in ext_lower:
        return "Twitter / X"
    if "reddit" in url_lower or "reddit" in ext_lower:
        return "Reddit"
    if "vimeo" in url_lower or "vimeo" in ext_lower:
        return "Vimeo"
    return "Web Video"


def make_safe_filename(raw_title, ext="mp4", max_len=60):
    """Creates a clean, safe filename free of newlines, path length issues, and illegal characters."""
    if not raw_title:
        return f"video.{ext}"
    cleaned = re.sub(r"[\r\n\t]+", " ", str(raw_title))
    cleaned = re.sub(r'[\\/*?:"<>|#%&{}\\<>*?/$!\'":@]', "", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    if len(cleaned) > max_len:
        cleaned = cleaned[:max_len].rstrip(" .")
    return f"{cleaned or 'video'}.{ext}"


def parse_video_formats(info):
    """Extracts, deduplicates, and organizes formats for any platform from top to low quality."""
    raw_formats = info.get("formats") or []
    duration = info.get("duration") or 0
    items = []

    # Handle single stream platforms
    if not raw_formats:
        ext = info.get("ext") or "mp4"
        items.append({
            "quality": "[Top Best Quality] Original Video + Audio",
            "ext": ext,
            "size": "Original Quality",
            "type": "Video + Audio",
            "format_spec": "best",
            "merge_format": ext if ext != "webm" else "mp4",
            "is_best": True,
            "is_audio_only": False,
        })
        return items

    max_audio_size = 0
    for f in raw_formats:
        if f.get("vcodec") == "none" and f.get("acodec") != "none":
            sz = f.get("filesize") or f.get("filesize_approx") or 0
            if not sz and duration and (f.get("tbr") or f.get("abr")):
                sz = int((f.get("tbr") or f.get("abr")) * 1000 / 8 * duration)
            if sz > max_audio_size:
                max_audio_size = sz

    # 1. Top Best Quality (Auto Best Video + Best Audio merged into MP4)
    items.append({
        "quality": "Top Best Quality (Auto Max Resolution)",
        "ext": "mp4",
        "size": "Best Available",
        "type": "Video + Audio (Merged)",
        "format_spec": "bestvideo+bestaudio/best",
        "merge_format": "mp4",
        "is_best": True,
        "is_audio_only": False,
    })

    # 2. Collect video streams
    valid_videos = [
        f for f in raw_formats
        if (f.get("vcodec") != "none" or f.get("video_ext") != "none")
        and f.get("ext") != "mhtml"
        and (f.get("height") or 0) > 0
    ]

    # Sort strictly from highest resolution to lowest, then bitrate/fps desc
    sorted_videos = sorted(
        valid_videos,
        key=lambda x: (
            x.get("height") or 0,
            x.get("tbr") or x.get("vbr") or 0,
            x.get("fps") or 0,
        ),
        reverse=True,
    )

    seen = set()
    video_items = []
    for f in sorted_videos:
        h = f.get("height") or 0
        fps = f.get("fps") or 30
        ext = f.get("ext") or "mp4"
        fid = f.get("format_id")
        acodec = f.get("acodec")
        has_audio = (acodec and acodec != "none") or f.get("audio_ext") not in (None, "none")

        key = (h, ext)
        if h > 0 and key in seen:
            continue
        if h > 0:
            seen.add(key)

        fps_label = f" {fps}fps" if fps and fps > 30 else ""
        res_tag = f"{h}p{fps_label}"

        if h >= 2160:
            res_tag += " (4K UHD)"
        elif h >= 1440:
            res_tag += " (2K QHD)"
        elif h >= 1080:
            res_tag += " (Full HD)"
        elif h >= 720:
            res_tag += " (HD)"
        elif 0 < h <= 480:
            res_tag += " (SD)"

        v_size = f.get("filesize") or f.get("filesize_approx") or 0
        if not v_size and duration and (f.get("tbr") or f.get("vbr")):
            v_size = int((f.get("tbr") or f.get("vbr")) * 1000 / 8 * duration)

        tot_size = v_size + (0 if has_audio else max_audio_size)
        size_str = f"{tot_size / (1024 * 1024):.1f} MB" if tot_size > 0 else "High Quality"

        if has_audio:
            fmt_spec = fid
            type_str = "Video + Audio"
        else:
            fmt_spec = f"{fid}+bestaudio/best"
            type_str = "Video + Audio (Merged)"

        video_items.append({
            "quality": res_tag,
            "ext": ext,
            "size": size_str,
            "type": type_str,
            "format_spec": fmt_spec,
            "merge_format": "mp4" if ext == "mp4" else ext,
            "is_best": False,
            "is_audio_only": False,
        })

    items.extend(video_items)

    # 3. Audio-Only Options
    audio_size_str = f"{max_audio_size / (1024 * 1024):.1f} MB" if max_audio_size > 0 else "Audio"
    items.append({
        "quality": "Best Audio Only (MP3)",
        "ext": "mp3",
        "size": audio_size_str,
        "type": "Audio Only (Converted)",
        "format_spec": "bestaudio/best",
        "merge_format": None,
        "is_best": False,
        "is_audio_only": True,
        "audio_codec": "mp3",
    })
    items.append({
        "quality": "Original Audio Stream (M4A/AAC)",
        "ext": "m4a",
        "size": audio_size_str,
        "type": "Audio Only (Original)",
        "format_spec": "bestaudio[ext=m4a]/bestaudio/best",
        "merge_format": None,
        "is_best": False,
        "is_audio_only": True,
        "audio_codec": None,
    })

    return items


# ==================== FLASK ROUTES ====================

@app.route("/")
def index():
    """Serves the main single page web application with fallback loaders."""
    try:
        return render_template("index.html")
    except Exception:
        for d in template_dirs:
            p = os.path.join(d, "index.html")
            if os.path.isfile(p):
                try:
                    with open(p, "r", encoding="utf-8") as f:
                        return f.read(), 200, {"Content-Type": "text/html; charset=utf-8"}
                except Exception:
                    pass
        return "<h1>OmniDownloader</h1><p>Template loading error. Please refresh.</p>", 200


@app.route("/favicon.ico")
def favicon():
    """Silences browser favicon requests with 204 No Content."""
    return "", 204


@app.route("/api/info", methods=["POST"])
def get_info():
    """Fetches video metadata and all available formats from any link."""
    data = request.get_json(force=True, silent=True) or {}
    url = (data.get("url") or "").strip()

    if not url:
        return jsonify({"error": "Please enter a valid video link."}), 400

    ffmpeg_bin = get_ffmpeg_path()
    cookie_file = get_cookie_file()

    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "extractor_args": {
            "youtube": {
                "player_client": ["tv_embedded", "ios_embedded", "android_creator"]
            }
        },
        "http_headers": {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        },
    }
    if ffmpeg_bin:
        ydl_opts["ffmpeg_location"] = ffmpeg_bin
    if cookie_file:
        ydl_opts["cookiefile"] = cookie_file

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)

        if "entries" in info:
            entries = list(info.get("entries") or [])
            if entries:
                info = entries[0]

        raw_title = info.get("title") or "Video Download"
        title = re.sub(r"[\r\n\t]+", " ", str(raw_title)).strip()
        if len(title) > 120:
            title = title[:120].strip() + "..."

        uploader = info.get("uploader") or info.get("channel") or info.get("creator") or "Unknown"
        thumbnail = info.get("thumbnail") or (info.get("thumbnails")[-1].get("url") if info.get("thumbnails") else "")
        duration_str = format_duration(info.get("duration"))
        platform = detect_platform(url, info.get("extractor_key"))

        formats = parse_video_formats(info)

        return jsonify({
            "url": url,
            "title": title,
            "raw_title": raw_title,
            "uploader": uploader,
            "thumbnail": thumbnail,
            "duration_str": duration_str,
            "platform": platform,
            "formats": formats,
        })
    except Exception as e:
        err = clean_ansi(str(e))
        if "Sign in to confirm you're not a bot" in err:
            err = (
                "YouTube flagged Vercel's cloud server as a bot. "
                "To download YouTube videos without limits, open the app locally at http://localhost:5000 "
                "or export a cookies.txt file to the project."
            )
        return jsonify({"error": err}), 500


@app.route("/api/download/start", methods=["POST"])
def start_download():
    """Starts an asynchronous download task for the selected format."""
    data = request.get_json(force=True, silent=True) or {}
    url = (data.get("url") or "").strip()
    format_spec = data.get("format_spec") or "bestvideo+bestaudio/best"
    merge_format = data.get("merge_format") or "mp4"
    is_audio_only = bool(data.get("is_audio_only"))
    audio_codec = data.get("audio_codec")
    raw_title = data.get("title") or "video"

    if not url:
        return jsonify({"error": "No URL provided"}), 400

    task_id = str(uuid.uuid4())
    TASKS[task_id] = {
        "state": "starting",
        "percent": 0.0,
        "speed": "-- MB/s",
        "stage": "Starting download...",
        "file_path": None,
        "filename": None,
        "error": None,
        "timestamp": time.time(),
    }

    thread = threading.Thread(
        target=_download_worker,
        args=(task_id, url, format_spec, merge_format, is_audio_only, audio_codec, raw_title),
        daemon=True,
    )
    thread.start()

    return jsonify({"task_id": task_id})


def _download_worker(task_id, url, format_spec, merge_format, is_audio_only, audio_codec, raw_title):
    """Background worker executing yt-dlp download with safe filenames."""
    task = TASKS[task_id]
    ffmpeg_bin = get_ffmpeg_path()

    task_dir = os.path.join(get_cache_dir(), task_id)
    os.makedirs(task_dir, exist_ok=True)

    outtmpl = os.path.join(task_dir, "media.%(ext)s")

    def progress_hook(d):
        if d.get("status") == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate")
            downloaded = d.get("downloaded_bytes", 0)
            pct = (downloaded / total * 100) if total else 0
            speed = d.get("speed")
            speed_str = f"{speed / 1024 / 1024:.2f} MB/s" if speed else "..."

            task["state"] = "downloading"
            task["percent"] = round(pct, 1)
            task["speed"] = speed_str
            task["stage"] = f"Downloading stream ({pct:.1f}%)..."
        elif d.get("status") == "finished":
            task["percent"] = 99.0
            task["stage"] = "Merging streams with FFmpeg..."

    cookie_file = get_cookie_file()
    ydl_opts = {
        "format": format_spec,
        "outtmpl": outtmpl,
        "noplaylist": True,
        "progress_hooks": [progress_hook],
        "quiet": True,
        "no_warnings": True,
        "extractor_args": {
            "youtube": {
                "player_client": ["tv_embedded", "ios_embedded", "android_creator"]
            }
        },
        "http_headers": {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        },
    }
    if cookie_file:
        ydl_opts["cookiefile"] = cookie_file

    if merge_format and not is_audio_only:
        ydl_opts["merge_output_format"] = merge_format

    if is_audio_only and audio_codec:
        ydl_opts["postprocessors"] = [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": audio_codec,
            "preferredquality": "192",
        }]

    if ffmpeg_bin:
        ydl_opts["ffmpeg_location"] = ffmpeg_bin

    try:
        task["stage"] = "Contacting video server..."
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            ydl.download([url])

        found_files = [
            f for f in os.listdir(task_dir)
            if not f.endswith(".part") and not f.endswith(".ytdl")
        ]
        if not found_files:
            raise Exception("Downloaded media file could not be found.")

        actual_file_name = found_files[0]
        actual_ext = actual_file_name.split(".")[-1] if "." in actual_file_name else "mp4"

        safe_name = make_safe_filename(raw_title, ext=actual_ext)

        final_file = os.path.join(task_dir, actual_file_name)
        task["state"] = "finished"
        task["percent"] = 100.0
        task["stage"] = "Download complete!"
        task["file_path"] = final_file
        task["filename"] = safe_name

    except Exception as e:
        task["state"] = "error"
        err = clean_ansi(str(e))
        if "Sign in to confirm you're not a bot" in err:
            err = (
                "YouTube flagged Vercel's cloud server as a bot. "
                "Open the app locally at http://localhost:5000 to download without restriction."
            )
        task["error"] = err
        task["stage"] = "Download failed."


@app.route("/api/download/progress/<task_id>")
def get_progress(task_id):
    """Returns real-time progress for a download task."""
    task = TASKS.get(task_id)
    if not task:
        return jsonify({"error": "Task not found"}), 404
    return jsonify({
        "state": task["state"],
        "percent": task["percent"],
        "speed": task["speed"],
        "stage": task["stage"],
        "error": task["error"],
    })


@app.route("/api/download/file/<task_id>")
def download_file(task_id):
    """Streams the finished file directly to the client's browser."""
    task = TASKS.get(task_id)
    if not task or not task.get("file_path") or not os.path.exists(task["file_path"]):
        return jsonify({"error": "File not found or still processing"}), 404

    return send_file(
        task["file_path"],
        as_attachment=True,
        download_name=task["filename"],
    )


def cleanup_old_files():
    """Periodically cleans up cache files older than 1 hour (runs on standalone server)."""
    while True:
        try:
            now = time.time()
            cache_dir = get_cache_dir()
            for tid, tinfo in list(TASKS.items()):
                if now - tinfo.get("timestamp", now) > 3600:
                    task_dir = os.path.join(cache_dir, tid)
                    if os.path.exists(task_dir):
                        shutil.rmtree(task_dir, ignore_errors=True)
                    TASKS.pop(tid, None)
        except Exception:
            pass
        time.sleep(1800)


if __name__ == "__main__":
    threading.Thread(target=cleanup_old_files, daemon=True).start()
    port = int(os.environ.get("PORT", 5000))
    print("=" * 60)
    print(" OmniDownloader Web Server Running!")
    print(f" Access your website at: http://localhost:{port}")
    print("=" * 60)
    app.run(host="0.0.0.0", port=port, debug=False)
