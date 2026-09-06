import os
import subprocess
import json
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from flask import Flask, render_template, send_file, jsonify, abort, Response, request
from collections import defaultdict
import time
app = Flask(__name__)

VIDEOS_DIR = Path("/app/videos")
THUMBS_DIR = Path("/app/thumbs")
CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "/app/data/config.json")
with open(CONFIG_PATH) as _f:
    _config = json.load(_f)
clipdetails = {"clipno":0}
WATCH_DIR = _config["watch_dir"]
# SAVE_PATH = _config["save_path"]
# if isinstance(SAVE_PATH, str):
#     SAVE_PATH = [SAVE_PATH]
CHUNK_PREFIX = _config["chunk_prefix"]
INIT_NAME = _config["init_name"]
UPLOAD_URL = _config["upload_url"]
PORT = _config["port"]
aftercliptime = _config["aftercliptime"]
beforecliptime = _config["beforecliptime"]
maxtimetomergemultipleclips = _config["maxtimetomergemultipleclips"]
TEMPPATH = "./"
HISTORY_RETENTION_SECONDS = 360  # drop buffered segments older than this
SEGMENT_WAIT_POLL_SECONDS = 0.2
SEGMENT_WAIT_TIMEOUT_SECONDS = 360  # give up waiting for a still-recording end_ts after this long
histories = {}  # parent_dir -> [{"path", "start", "end"}, ...] oldest first
histories_lock = threading.Lock()
@app.template_filter('timestamp')
def format_timestamp(timestamp):
    """Format Unix timestamp to readable time"""
    return datetime.fromtimestamp(timestamp).strftime('%H:%M:%S')

VIDEO_EXTENSIONS = {'.mp4', '.avi', '.mkv', '.mov', '.wmv', '.flv', '.webm', '.m4v'}

# Database path for caching video metadata
DB_PATH = Path("/app/data/video_cache.db")
db_lock = threading.Lock()


def init_db():
    """Initialize SQLite database for caching video durations"""
    # Ensure data directory exists
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)

    conn = sqlite3.connect(str(DB_PATH), timeout=10.0)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS video_durations (
            video_name TEXT PRIMARY KEY,
            duration REAL NOT NULL,
            mtime REAL NOT NULL,
            updated_at REAL NOT NULL
        )
    ''')
    cursor.execute(
        """CREATE TABLE IF NOT EXISTS videos (
    id INTEGER PRIMARY KEY
    )"""
    )
    conn.commit()
    conn.close()

def is_target(path):
    name = os.path.basename(path)
    return (
        name.startswith(CHUNK_PREFIX)
        and name.endswith(".m4s")
        and not name.endswith(".m4s.tmp")
    )


def getvideo(path):
    return path.replace("stream1", "stream0")


def init_for(chunk_path):
    # chunk-stream{N}-{seq}.m4s -> init-stream{N}.m4s in same dir
    stream_part = os.path.basename(chunk_path).split("-", 2)[1]
    return os.path.join(os.path.dirname(chunk_path), f"init-{stream_part}.m4s")


WATCH_POLL_SECONDS = 0.5
WATCH_ACTIVE_WINDOW_SECONDS = 60  # stop polling a session dir once it's gone quiet this long

def main():
    os.makedirs(WATCH_DIR, exist_ok=True)
    print(f"watching {WATCH_DIR}/ recursively")

    # WATCH_DIR is a network mount (CIFS over Tailscale to the recording PC)
    # that can vanish/reappear whenever that machine reboots, so every OS
    # call against it needs to tolerate transient failures without killing
    # this thread. We still avoid a full recursive stat scan (like
    # watchdog's PollingObserver) since with thousands of accumulated
    # historical files that's needlessly expensive; a readdir-only diff of
    # recently-touched session dirs is enough.
    known = {}  # session dir -> set of filenames already handled
    try:
        while True:
            try:
                try:
                    with os.scandir(WATCH_DIR) as it:
                        dirs = [e for e in it if e.is_dir(follow_symlinks=False)]
                except OSError as e:
                    print(f"watch: {WATCH_DIR} unavailable ({e})")
                    dirs = []

                now = time.time()
                for entry in dirs:
                    try:
                        mtime = entry.stat().st_mtime
                    except OSError:
                        continue

                    # Once a dir is baselined, its bookkeeping is kept for the
                    # life of the process (a growing set of filenames costs
                    # nothing at this scale) - the active-window check only
                    # skips *re-listing* a quiet dir, it must never forget
                    # what's already been recorded, or a dir that goes quiet
                    # and later resumes gets silently re-baselined and its
                    # new segments never get recorded.
                    if now - mtime > WATCH_ACTIVE_WINDOW_SECONDS and entry.path in known:
                        continue

                    is_new_dir = entry.path not in known
                    seen = known.setdefault(entry.path, set())
                    try:
                        with os.scandir(entry.path) as it:
                            names = [e.name for e in it]
                    except OSError:
                        continue

                    if is_new_dir:
                        # baseline: don't backfill files that already existed
                        # before we started watching this dir
                        seen.update(names)
                        continue

                    for name in names:
                        if name in seen:
                            continue
                        seen.add(name)
                        full_path = os.path.join(entry.path, name)
                        if is_target(full_path):
                            # print("meow", full_path)
                            record_segment(full_path)
            except Exception as e:
                print(f"watch loop error: {e}")

            time.sleep(WATCH_POLL_SECONDS)
    except KeyboardInterrupt:
        print("\nstopped")
def record_segment(path):
    parent = os.path.dirname(path)
    now = time.time()
    with histories_lock:
        hist = histories.setdefault(parent, [])
        start = hist[-1]["end"] if hist else now
        hist.append({"path": path, "start": start, "end": now})
        cutoff = now - HISTORY_RETENTION_SECONDS
        while hist and hist[0]["end"] < cutoff:
            hist.pop(0)


def concat(init_path, chunks, out):
    with open(out, "wb") as o, open(init_path, "rb") as i:
        o.write(i.read())
        for c in chunks:
            with open(c, "rb") as f:
                o.write(f.read())


def _remove_with_retry(path, attempts=10, delay=0.2):
    for i in range(attempts):
        try:
            os.remove(path)
            return
        except PermissionError:
            if i == attempts - 1:
                raise
            time.sleep(delay)

def _render_clip(audio_paths, video_paths, name, beginrelative, endrelative):
    unique = f"{name}_{os.getpid()}_{threading.get_ident()}"
    audio_tmp = os.path.join(TEMPPATH, f".tmp_audio_{unique}.mp4")
    video_tmp = os.path.join(TEMPPATH, f".tmp_video_{unique}.mp4")
    concat(init_for(audio_paths[0]), audio_paths, audio_tmp)
    concat(init_for(video_paths[0]), video_paths, video_tmp)

    # Naive init+chunks concat leaves session-relative decode times in every
    # moof, so ffmpeg's input -ss can't seek by sample. Decode the whole
    # concat sequentially and use setpts+trim to renormalise PTS to 0 first.
    filter_complex = (
        f"[0:v]setpts=PTS-STARTPTS,"
        f"trim=start={beginrelative}:end={endrelative},"
        f"setpts=PTS-STARTPTS[v];"
        f"[1:a]asetpts=PTS-STARTPTS,"
        f"atrim=start={beginrelative}:end={endrelative},"
        f"asetpts=PTS-STARTPTS[a]"
    )
    VIDEOS_DIR.mkdir(parents=True, exist_ok=True)
    output_path = os.path.join(TEMPPATH, f"{VIDEOS_DIR}/{name}.mp4")
    subprocess.run(
        ["ffmpeg", "-y",
         "-loglevel", "quiet",
         "-i", video_tmp, "-i", audio_tmp,
         "-filter_complex", filter_complex,
         "-map", "[v]", "-map", "[a]",
         "-c:v", "libx264", "-c:a", "aac", "-b:a", "192k",
         output_path],
        check=True,
    )
    # try:
    #     with open(output_path, "rb") as f:
    #         resp = requests.post(f"{UPLOAD_URL}/upload", data=f, params={"name": name})
    #     resp.raise_for_status()
    #     print(f"sent {name} to {UPLOAD_URL}")
    #     _remove_with_retry(output_path)
    # except Exception as e:
    #     print(f"Failed to send {name} to {UPLOAD_URL}: {e}")
       
    _remove_with_retry(audio_tmp)
    _remove_with_retry(video_tmp)

def get_cached_duration(video_name, mtime):
    """Get cached duration if exists and mtime matches"""
    try:
        conn = sqlite3.connect(str(DB_PATH), timeout=10.0)
        cursor = conn.cursor()
        cursor.execute(
            'SELECT duration, mtime FROM video_durations WHERE video_name = ?',
            (video_name,)
        )
        result = cursor.fetchone()
        conn.close()

        if result and result[1] == mtime:
            return result[0]
        return None
    except Exception as e:
        print(f"Error reading cache for {video_name}: {e}")
        return None


def set_cached_duration(video_name, duration, mtime):
    """Store duration in cache with current mtime"""
    try:
        conn = sqlite3.connect(str(DB_PATH), timeout=10.0)
        cursor = conn.cursor()
        cursor.execute(
            '''INSERT OR REPLACE INTO video_durations
               (video_name, duration, mtime, updated_at)
               VALUES (?, ?, ?, ?)''',
            (video_name, duration, mtime, datetime.now().timestamp())
        )
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"Error caching duration for {video_name}: {e}")


def save_clip(start_ts, end_ts, name):
    """Save every buffered recording session covering [start_ts, end_ts] as {name}.mp4.

    Blocks until end_ts has passed and its segment has actually been recorded,
    so callers can pass an end_ts that's still in the future (e.g. "clip until
    a few seconds from now").
    """
    now = time.time()
    if end_ts > now:
        time.sleep(end_ts - now)

    deadline = time.time() + SEGMENT_WAIT_TIMEOUT_SECONDS
    while time.time() < deadline:
        with histories_lock:
            if any(hist and hist[-1]["end"] >= end_ts for hist in histories.values()):
                break
        time.sleep(SEGMENT_WAIT_POLL_SECONDS)

    with histories_lock:
        snapshot = {parent: list(hist) for parent, hist in histories.items()}

    saved_any = False
    for hist in snapshot.values():
        matches = [seg for seg in hist if seg["end"] > start_ts and seg["start"] < end_ts]
        if not matches:
            continue
        audio_paths = [seg["path"] for seg in matches]
        video_paths = [getvideo(p) for p in audio_paths]
        clip_start = matches[0]["start"]
        beginrelative = start_ts - clip_start
        endrelative = end_ts - clip_start
        threading.Thread(
            target=_render_clip,
            args=(audio_paths, video_paths, name, beginrelative, endrelative),
            daemon=True,
        ).start()
        saved_any = True
    if not saved_any:
        print(f"save_clip: no buffered segments cover {start_ts}-{end_ts}")
    return saved_any

def save_clip_buffer(start, end, desiredname,clipno):
    global clipdetails
    with histories_lock:
        clipdetails["end"] = max(clipdetails.get("end",end),end) 
        clipdetails["start"] = min(clipdetails.get("start",start) or start,start)
        clipdetails["name"] = clipdetails.get("name",desiredname) or desiredname
    # print(max(end+maxtimetomergemultipleclips - time.time(),0))
    time.sleep(max(end+maxtimetomergemultipleclips - time.time(),0))
    if clipdetails["clipno"] != clipno:
        print("concatanting")
        return clipdetails.get("name",desiredname),start - clipdetails["start"] + beforecliptime
    # print("saving")
    start =  min(clipdetails.get("start",start),start)
    end = max(clipdetails.get("end",end),end) 
    desiredname =  clipdetails.get("name",desiredname)

    with histories_lock:
        clipdetails["start"] = False
        clipdetails["name"] = False
    print("saving",desiredname)
    save_clip(start,end,desiredname)
    return desiredname , end - start - aftercliptime

def get_video_duration(video_path):
    """Get video duration using ffprobe"""
    try:
        result = subprocess.run([
            'ffprobe',
            '-v', 'error',
            '-show_entries', 'format=duration',
            '-of', 'json',
            str(video_path)
        ], capture_output=True, text=True, timeout=5)

        data = json.loads(result.stdout)
        duration = float(data['format']['duration'])
        return duration
    except Exception as e:
        print(f"Error getting duration for {video_path}: {e}")
        return 0.0


def get_video_files():
    """Scan videos directory and return list of video files with metadata"""
    videos = []

    if not VIDEOS_DIR.exists():
        return videos

    for file_path in VIDEOS_DIR.rglob("*"):
        if file_path.is_file() and file_path.suffix.lower() in VIDEO_EXTENSIONS:
            try:
                stat = file_path.stat()

                # Check cache first
                duration = get_cached_duration(file_path.name, stat.st_mtime)

                # If not cached or mtime changed, compute and cache it
                if duration is None:
                    duration = get_video_duration(file_path)
                    set_cached_duration(file_path.name, duration, stat.st_mtime)

                videos.append({
                    'name': file_path.name,
                    'path': str(file_path.relative_to(VIDEOS_DIR)),
                    'mtime': stat.st_mtime,
                    'size': stat.st_size,
                    'duration': duration
                })
            except Exception as e:
                print(f"Error reading {file_path}: {e}")

    return videos


def group_videos_by_date(videos):
    """Group videos by creation date"""
    grouped = defaultdict(list)

    for video in videos:
        date = datetime.fromtimestamp(video['mtime']).strftime('%Y-%m-%d')
        grouped[date].append(video)

    # Sort dates in reverse (newest first)
    sorted_groups = sorted(grouped.items(), key=lambda x: x[0], reverse=True)

    return sorted_groups


def generate_thumbnail(video_path):
    """Generate thumbnail for video using ffmpeg on-the-fly"""
    try:
        result = subprocess.run([
            'ffmpeg',
            '-i', str(video_path),
            '-ss', '00:00:5.4',
            '-vframes', '1',
            '-vf', 'scale=320:-1',
            '-f', 'image2pipe',
            '-vcodec', 'mjpeg',
            'pipe:1'
        ], check=True, capture_output=True, timeout=10)
        return result.stdout
    except Exception as e:
        print(f"Error generating thumbnail for {video_path}: {e}")
        return None


@app.route('/')
def index():
    """Main page showing video collage"""
    # print("weee")
    videos = get_video_files()
    # print("meow")
    grouped_videos = group_videos_by_date(videos)
    # print("wooo")
    return render_template('index.html', grouped_videos=grouped_videos)


@app.route('/api/videos')
def api_videos():
    """API endpoint returning videos grouped by date"""
    videos = get_video_files()
    grouped_videos = group_videos_by_date(videos)

    result = []
    for date, vids in grouped_videos:
        result.append({
            'date': date,
            'videos': (vids)
        })

    return jsonify((result))


@app.route('/thumbnail/<path:video_path>')
def thumbnail(video_path):
    """Generate and serve thumbnail for video, caching it to THUMBS_DIR"""
    # Sanitize path
    if '..' in video_path or video_path.startswith('/'):
        abort(403)

    video_file = VIDEOS_DIR / video_path
    if not video_file.exists():
        abort(404)

    thumb_file = THUMBS_DIR / Path(video_path).with_suffix('.jpg')

    # Regenerate if missing or stale relative to the source video
    if not thumb_file.exists() or thumb_file.stat().st_mtime < video_file.stat().st_mtime:
        thumbnail_data = generate_thumbnail(video_file)
        if thumbnail_data is None:
            abort(500)

        thumb_file.parent.mkdir(parents=True, exist_ok=True)
        thumb_file.write_bytes(thumbnail_data)

    return send_file(thumb_file, mimetype='image/jpeg')

@app.route("/upload", methods=["POST"])
def uploadvideo():
    """Receive a rendered clip and save it into the video library"""
    name = request.args.get("name")
    if not name:
        abort(400)

    filename = os.path.basename(name)
    if not filename or filename in (".", ".."):
        abort(400)
    if Path(filename).suffix.lower() not in VIDEO_EXTENSIONS:
        filename += ".mp4"

    VIDEOS_DIR.mkdir(parents=True, exist_ok=True)
    dest_path = VIDEOS_DIR / filename

    with open(dest_path, "wb") as out:
        while True:
            chunk = request.stream.read(1024 * 1024)
            if not chunk:
                break
            out.write(chunk)

    return {"okay": "yes"}


@app.route("/db",methods=["POST"])
def dbinsert(data = None):
    conn = sqlite3.connect(str(DB_PATH), timeout=10.0)
    c = conn.cursor()
    columns = [row[1] for row in c.execute("PRAGMA table_info(videos)")]
    stuff = data or request.get_json()["vars"]
    for thing,value in stuff.items():
        if thing not in columns:
            c.execute(f"ALTER TABLE videos ADD COLUMN {thing} {isinstance(value,int) and 'INTEGER' or 'TEXT'}")
    c.execute(f"INSERT INTO videos ({', '.join(list(stuff.keys()))}) VALUES ({', '.join(['?']*len(stuff))})",list(map(json.dumps,stuff.values())))
    conn.commit()
    conn.close()  
    return {"okay":"yes"}

@app.route("/killlog", methods=["POST"])
def logakill():
    global clipdetails
    # print("Meow")
    # return {"moew":True}
    with histories_lock:
        clipdetails["clipno"] += 1
        clipno = clipdetails["clipno"]
    stuff = request.get_json()
    # stuff["timestamp"] = time.time() if abs(time.time() - stuff["timestamp"]) < 1 else stuff["timestamp"] # more accurate than titanfall can do soo seems good
    print("got a new kill!",stuff.get("attackername",False),"killed",stuff["victimname"])
    
    # print(json.dumps(stuff,indent=4))
    # query = sqlite3.connect("./database.db")
    # c = query.cursor()
    # columns = [row[1] for row in c.execute("PRAGMA table_info(videos)")]
    stuff["name"],stuff["whereinclip"]  = save_clip_buffer(stuff["timestamp"] - beforecliptime, stuff["timestamp"] + aftercliptime,f"{stuff['attackerweaponname']}_{stuff['localisattacker']}_{stuff['timestamp']}_{clipno}",clipno)
    # resp = requests.post(f"{UPLOAD_URL}/db", json={"things":f"INSERT INTO videos ({", ".join(list(stuff.keys()))}) VALUES ({", ".join(["?"]*len(stuff))})","vars":stuff})
    dbinsert({"things":f"INSERT INTO videos ({', '.join(list(stuff.keys()))}) VALUES ({', '.join(['?']*len(stuff))})","vars":stuff})
    # print(f"sent a database entry for {stuff["name"]}")
    # resp.raise_for_status()
    # for thing,value in stuff.items():
    #     if thing not in columns:
    #         c.execute(f"ALTER TABLE videos ADD COLUMN {thing} {isinstance(value,int) and "INTEGER" or "TEXT"}")
    # c.execute(f"INSERT INTO videos ({", ".join(list(stuff.keys()))}) VALUES ({", ".join(["?"]*len(stuff))})",list(map(json.dumps,stuff.values())))
    # query.commit()
    # query.close()  
    return {"moew":True}


@app.route('/video/<path:video_path>')
def video(video_path):
    """Serve video file"""
    # Sanitize path
    if '..' in video_path or video_path.startswith('/'):
        abort(403)

    video_file = VIDEOS_DIR / video_path
    if not video_file.exists():
        abort(404)

    return send_file(video_file)


if __name__ == '__main__':
    from waitress import serve
    
    print("Initializing database...")
    init_db()
    threading.Thread(target=main,daemon=True).start()
    print("Starting Waitress server on http://0.0.0.0:5000")
    serve(app, host='0.0.0.0', port=5000, threads=60)