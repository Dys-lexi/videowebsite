import os
import subprocess
import json
import sqlite3
import threading
import random
from datetime import datetime
from pathlib import Path
from flask import Flask, render_template, send_file, jsonify, abort, Response, request
from collections import defaultdict
import time
app = Flask(__name__)
import inspect
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
HISTORY_RETENTION_SECONDS = 60  # drop buffered segments older than this
SEGMENT_WAIT_POLL_SECONDS = 0.2
SEGMENT_WAIT_TIMEOUT_SECONDS = 60  # give up waiting for a still-recording end_ts after this long
histories = {}  # parent_dir -> [{"path", "start", "end"}, ...] oldest first
histories_lock = threading.Lock()

# The uploader and this server don't share a clock. Every /killlog request
# implicitly samples (server_receive_time - uploader_reported_time), which
# equals (clock skew) + (network/processing latency). Latency can only add
# to that sample, never subtract from it, so the smallest sample ever seen
# is the best available estimate of the pure clock skew - track a running
# minimum and use it to translate every uploader timestamp onto the
# server's clock before it's used to look up recorded video segments.
clock_skew_lock = threading.Lock()
clock_skew = None  # server_time - uploader_time, seconds
# CIFS exposes the recorder's mtimes with the recorder's clock offset.  Learn
# that offset from new chunks as they arrive so crash recovery can translate
# their mtimes onto this server's Unix timeline.
recording_clock_skew_lock = threading.Lock()
recording_clock_skew = None  # server arrival time - recorder file mtime
recording_clock_skew_ready = threading.Event()
realprint = print

DISALLOWED_COLOURS = (
    0,
    52,
    16,
    18,
    17,
    20,
    23,
    25,
    24,
    59,
    60,
    62,
    61,
    58,
    65,
    95,
    61,
    54,
    92,
    102,
    101,
    232,
    233,
    234,
    235,
    236,
    237,
    238,
    239,
    240,
    57,
    56,
    19,
    91,
    89,
    90,
    88,
    96,
    53
)

linecolours = {}
lastfuncline = ""
def print(*message, end="\033[0m\n",function = None,line=None):
    global linecolours, lastfuncline
    message = (
        " ".join([str(i) for i in message])
        .replace("[110m", "[38;2;200;200;200m")
        .replace("[111m", "[38;2;80;229;255m")
        .replace("[112m", "[38;2;213;80;16m")
    )


    function = function or  str(inspect.currentframe().f_back.f_code.co_name)
    line = line or str(inspect.currentframe().f_back.f_lineno)
    if line not in linecolours:
        while True:
            colour = random.randint(0, 255)
            if colour not in DISALLOWED_COLOURS:
                break
        linecolours[line] = colour
    currentfuncline = f"{line},{function}"
    if False:
        realprint(
            f"[0m{(('[' + function[:9].ljust(9) + ']') if currentfuncline != lastfuncline else '⯈'.ljust(11))}{('[' + line.ljust(3) + ']')}[{datetime.now().strftime('%H:%M:%S %d/%m')}] {message}"
        )
    else:
        realprint(
            f"[38;2;215;22;105m{(('[' + function[:9].ljust(9) + ']') if currentfuncline != lastfuncline else '⯈'.ljust(11))}[38;2;126;89;140m{('[' + line.ljust(3) + ']')}[38;2;27;64;152m[{datetime.now().strftime('%H:%M:%S %d/%m')}][38;5;{linecolours[line]}m {(message)}",
            end=end,
        )
    lastfuncline = currentfuncline

def correct_uploader_timestamp(uploader_ts):
    global clock_skew
    sample = time.time() - uploader_ts
    with clock_skew_lock:
        clock_skew = sample if clock_skew is None else min(clock_skew, sample)
        skew = clock_skew
    return uploader_ts + skew
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
    # cursor.execute("DROP TABLE  video_durations")
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

                # Compare directory mtimes with one another, not with this
                # machine's clock.  The CIFS recorder can be an hour (or more)
                # out, but its newest directory is still the active one.
                dir_mtimes = []
                for entry in dirs:
                    try:
                        mtime = entry.stat().st_mtime
                    except OSError:
                        continue
                    dir_mtimes.append((entry, mtime))

                newest_mtime = max((mtime for _entry, mtime in dir_mtimes), default=None)
                for entry, mtime in dir_mtimes:

                    # Once a dir is baselined, its bookkeeping is kept for the
                    # life of the process (a growing set of filenames costs
                    # nothing at this scale) - the active-window check only
                    # skips *re-listing* a quiet dir, it must never forget
                    # what's already been recorded, or a dir that goes quiet
                    # and later resumes gets silently re-baselined and its
                    # new segments never get recorded.
                    if (newest_mtime is not None
                            and newest_mtime - mtime > WATCH_ACTIVE_WINDOW_SECONDS
                            and entry.path in known):
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
    global recording_clock_skew
    parent = os.path.dirname(path)
    now = time.time()
    try:
        sample = now - os.stat(path).st_mtime
    except OSError:
        sample = None
    if sample is not None:
        # File creation/visibility latency can only increase the sample, so the
        # running minimum is the closest estimate of the actual clock skew.
        with recording_clock_skew_lock:
            recording_clock_skew = (
                sample if recording_clock_skew is None
                else min(recording_clock_skew, sample)
            )
        recording_clock_skew_ready.set()
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

# At most this many renders run at once. Each _render_clip() spawns an ffmpeg
# libx264 re-encode and reads whole segment files into memory, so an unbounded
# burst of kills (or boot-time reconstruction racing live saves) could exhaust
# CPU/RAM. Extra callers block on the semaphore until a slot frees up.
MAX_CONCURRENT_RENDERS = 2
_render_semaphore = threading.BoundedSemaphore(MAX_CONCURRENT_RENDERS)


def _render_clip(audio_paths, video_paths, name, beginrelative, endrelative):
    with _render_semaphore:
        _render_clip_inner(audio_paths, video_paths, name, beginrelative, endrelative)


def _render_clip_inner(audio_paths, video_paths, name, beginrelative, endrelative):
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
    print("saved",name)
    _remove_with_retry(audio_tmp)
    _remove_with_retry(video_tmp)
    makethumb(f"{name}.mp4")
    duration = get_video_duration(output_path)
    set_cached_duration(f"{name}.mp4", duration, int(time.time()))

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

        # if result and result[1] == mtime:
        if not result: return result
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

def save_clip_buffer(start, end, desiredname,clipno,localisattacker):
    global clipdetails
    with histories_lock:
        clipdetails["end"] = max(clipdetails.get("end",end),end) 
        clipdetails["start"] = min(clipdetails.get("start",start) or start,start)
        clipdetails["name"] = clipdetails.get("name",desiredname) or desiredname
        starttime = clipdetails["start"] 
        name = clipdetails["name"]
    # print(max(end+maxtimetomergemultipleclips - time.time(),0))
    if localisattacker: #avoid gluing together clips after a death
        time.sleep(max(end+maxtimetomergemultipleclips - time.time(),0))
        if clipdetails["clipno"] != clipno:
            print("concatanting")
            return name,start - starttime + beforecliptime
    else:
        time.sleep(max(end+2 - time.time(),0))
        if clipdetails["clipno"] != clipno:
            print("concatanting")
            return name,start - starttime + beforecliptime
    # print("saving")
    with histories_lock:
        start =  min(clipdetails.get("start",start),start)
        end = max(clipdetails.get("end",end),end) 
        desiredname =  clipdetails.get("name",desiredname)
        clipdetails["start"] = False
        clipdetails["name"] = False
    print("saving",desiredname)
    save_clip(start,end,desiredname)
    return desiredname , end - start - aftercliptime




WATCH_DIR_WAIT_SECONDS = 30  


def _is_clip_name(name):
    """A real clip name is "{weapon}_{localisattacker}_{timestamp}_{clipno}".

    Filters out the occasional garbage row (e.g. name "0") produced by the
    merge-buffer race in save_clip_buffer, which has no clip to rebuild."""
    if not isinstance(name, str) or not name:
        return False
    parts = name.rsplit("_", 2)
    if len(parts) != 3:
        return False
    _, ts_part, clipno_part = parts
    try:
        float(ts_part)
        int(clipno_part)
    except ValueError:
        return False
    return True


def _chunk_seq(name):
    # chunk-stream1-{seq}.m4s -> seq (int) for ordering the concat correctly
    try:
        return int(name.rsplit("-", 1)[1].split(".", 1)[0])
    except (IndexError, ValueError):
        return None


def _session_dir_segments(session_dir):
    """Rebuild [start, end] timing for every audio chunk in one recording-session
    dir from file mtimes, ordered by DASH sequence number so the concat order is
    always correct even if mtimes are non-monotonic. Mirrors record_segment():
    a chunk's end is its own mtime, its start is the previous chunk's end."""
    try:
        names = os.listdir(session_dir)
    except OSError:
        return []
    with recording_clock_skew_lock:
        mtime_skew = recording_clock_skew or 0.0

    audio = []
    for n in names:
        full = os.path.join(session_dir, n)
        if not is_target(full):
            continue
        seq = _chunk_seq(n)
        if seq is None:
            continue
        try:
            mtime = os.stat(full).st_mtime
        except OSError:
            continue
        audio.append((seq, full, mtime + mtime_skew))
    audio.sort(key=lambda x: x[0])

    segs = []
    prev_end = None
    for _seq, full, mtime in audio:
        start = prev_end if prev_end is not None else mtime
        segs.append({"path": full, "start": start, "end": mtime})
        prev_end = mtime
    return segs


def _matches_for_window(segs, start_ts, end_ts):
    # same overlap test the live save_clip() uses to pick covering segments
    return [s for s in segs if s["end"] > start_ts and s["start"] < end_ts]


def _find_segments_for_clip(start_ts, end_ts):
    """Scan every recording-session dir under WATCH_DIR and return the matching
    audio segments from the dir that best covers [start_ts, end_ts], or [] if the
    required snippets are no longer on disk (chunks rotated away / mount gone)."""
    try:
        entries = os.listdir(WATCH_DIR)
    except OSError as e:
        print(f"reconstruct: watch dir {WATCH_DIR} unavailable ({e})")
        return []

    best, best_cov = [], 0.0
    for d in entries:
        session_dir = os.path.join(WATCH_DIR, d)
        if not os.path.isdir(session_dir):
            continue
        matches = _matches_for_window(_session_dir_segments(session_dir), start_ts, end_ts)
        if not matches:
            continue
        # coverage = how much of the requested window this session actually spans
        cov = max(0.0, min(matches[-1]["end"], end_ts) - max(matches[0]["start"], start_ts))
        if cov > best_cov:
            best, best_cov = matches, cov
    return best


def _wait_for_watch_dir():
    deadline = time.time() + WATCH_DIR_WAIT_SECONDS
    while time.time() < deadline:
        try:
            os.listdir(WATCH_DIR)
            return True
        except OSError:
            time.sleep(1.0)
    return False


def _clip_is_complete(name):
    """True if {name}.mp4 already exists as a fully-rendered file. Uses the
    duration cache (written on _render_clip's last line) so we don't ffprobe the
    hundreds of good clips on every boot - only files missing from the cache get
    probed, and a valid one is cached so the next boot skips it too."""
    out = VIDEOS_DIR / f"{name}.mp4"
    try:
        if not out.exists() or out.stat().st_size == 0:
            return False
        cached = get_cached_duration(f"{name}.mp4", out.stat().st_mtime)
        if cached is not None:
            return cached > 0
        duration = get_video_duration(out)
        if duration and duration > 0:
            set_cached_duration(f"{name}.mp4", duration, int(out.stat().st_mtime))
            return True
        return False  # exists but ffprobe found no valid stream -> truncated render
    except OSError:
        return False


def reconstruct_missing_clips():
    """On boot, re-render any clip whose DB rows exist but whose output .mp4 is
    missing or was left truncated by a crash mid-render.

    Every kill event that was merged into a clip shares that clip's name in the
    videos table, so grouping rows by name reproduces the merge-close-together
    grouping for free; the clip window is then rebuilt from the group's kill
    timestamps (min - beforecliptime .. max + aftercliptime) exactly as
    save_clip_buffer/save_clip would have produced it."""
    try:
        conn = sqlite3.connect(str(DB_PATH), timeout=10.0)
        c = conn.cursor()
        try:
            rows = c.execute("SELECT name, timestamp FROM videos").fetchall()
        except sqlite3.OperationalError:
            rows = []
        conn.close()
    except Exception as e:
        print(f"reconstruct: cannot read database ({e})")
        return

    groups = defaultdict(list)
    for name, ts in rows:
        if not _is_clip_name(name):
            continue
        try:
            groups[name].append(float(ts))
        except (TypeError, ValueError):
            continue

    # Only the clips whose output is actually missing/broken need rebuilding.
    todo = []
    for name, timestamps in groups.items():
        if _clip_is_complete(name):
            continue
        start_ts = min(timestamps) - beforecliptime
        end_ts = max(timestamps) + aftercliptime
        todo.append((name, start_ts, end_ts))

    if not todo:
        print("reconstruct: no interrupted clips to rebuild")
        return

    print(f"reconstruct: {len(todo)} clip(s) missing an output, checking snippets")
    if not _wait_for_watch_dir():
        print(f"reconstruct: {WATCH_DIR} never became available, giving up")
        return

    # Prefer a clock-skew sample from a newly-created chunk.  If recording is
    # currently idle the wait expires and recovery falls back to raw mtimes.
    if not recording_clock_skew_ready.wait(WATCH_DIR_WAIT_SECONDS):
        print("reconstruct: no new chunk arrived to calibrate recorder mtimes")

    rebuilt = 0
    for name, start_ts, end_ts in todo:
        try:
            matches = _find_segments_for_clip(start_ts, end_ts)
            if not matches:
                print(f"reconstruct: snippets no longer on disk for {name}, skipping")
                continue
            audio_paths = [s["path"] for s in matches]
            video_paths = [getvideo(p) for p in audio_paths]
            clip_start = matches[0]["start"]
            beginrelative = start_ts - clip_start
            endrelative = end_ts - clip_start
            print(f"reconstruct: rebuilding {name}")
            _render_clip(audio_paths, video_paths, name, beginrelative, endrelative)
            rebuilt += 1
        except Exception as e:
            print(f"reconstruct: failed to rebuild {name}: {e}")
    print(f"reconstruct: rebuilt {rebuilt}/{len(todo)} clip(s)")


# ---------------------------------------------------------------------------
# One-shot repair for rows corrupted by the old save_clip_buffer death-race.
#
# When the local player died mid-clip, clipdetails["name"]/["start"] were reset
# to False before an *earlier* kill in that same clip woke from its merge-sleep.
# That kill was then written with name = False (stored as "0") and
# whereinclip = start - False + beforecliptime, i.e. a raw unix timestamp.
#
# Every kill merged into one clip shares that clip's name and a common clip
# start, and the name embeds the clip's first-kill timestamp. So a corrupted
# row can be healed by finding a healthy row from the same clip and copying its
# name across:
#   * same clip == same recording session (timestamp - gametime is a constant
#     per match: the match's real-world start) AND close together in timestamp
#     (within the same aftercliptime + maxtimetomergemultipleclips merge window
#     save_clip_buffer used to glue kills together).
#   * whereinclip is then rebuilt as timestamp - clip_start + beforecliptime,
#     where clip_start is the earliest timestamp across the whole clip (healthy
#     rows *and* the rows being healed - a corrupted row is often the clip's
#     own first kill, which is why its int timestamp is the one in the name).
# The timings all come from config.json (beforecliptime / aftercliptime /
# maxtimetomergemultipleclips), so this stays correct if they are retuned.
# ---------------------------------------------------------------------------

def repair_corrupted_clip_names():
    # A match's (timestamp - gametime) is fixed; allow a little float/skew slack
    # so two kills are judged same-session only if their offsets basically agree.
    SESSION_OFFSET_TOL = 2.0
    # Two kills merged into one clip are never further apart than the window
    # save_clip_buffer waited before closing a clip.
    MERGE_WINDOW = aftercliptime + maxtimetomergemultipleclips

    def _num(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    try:
        conn = sqlite3.connect(str(DB_PATH), timeout=10.0)
        c = conn.cursor()
        try:
            rows = c.execute(
                "SELECT id, name, timestamp, gametime, whereinclip FROM videos"
            ).fetchall()
        except sqlite3.OperationalError:
            conn.close()
            print("repair: videos table has no rows to check")
            return
    except Exception as e:
        print(f"repair: cannot open database ({e})")
        return

    healthy, corrupted = [], []
    for rid, name, ts, gametime, whereinclip in rows:
        t, g, w = _num(ts), _num(gametime), _num(whereinclip)
        if t is None or g is None:
            continue  # can't place a row on a timeline without both clocks
        if _is_clip_name(name):
            healthy.append((rid, name, t, g))
        elif w is not None and w > 1e8:
            # name isn't a real clip name and whereinclip is a unix-scale
            # timestamp rather than a small in-clip offset -> the death-race row.
            corrupted.append((rid, t, g))

    if not corrupted:
        conn.close()
        print("repair: no corrupted clip-name rows found")
        return

    # For each corrupted row pick the nearest healthy kill from the same session.
    assigned = {}   # row id -> clip name
    orphans = []
    for rid, t, g in corrupted:
        offset = t - g
        best_name, best_dist = None, None
        for _hid, hname, ht, hg in healthy:
            if abs((ht - hg) - offset) > SESSION_OFFSET_TOL:
                continue
            dist = abs(ht - t)
            if best_dist is None or dist < best_dist:
                best_name, best_dist = hname, dist
        if best_name is None or best_dist > MERGE_WINDOW:
            orphans.append(rid)  # lone death, no sibling kill to borrow a name from
            continue
        assigned[rid] = best_name

    if not assigned:
        conn.close()
        print(f"repair: {len(corrupted)} corrupted row(s) but none had a sibling clip; left untouched")
        return

    # clip_start = earliest timestamp across the whole clip, including the rows
    # we're about to heal (a corrupted row may be the clip's own first kill).
    clip_start = {}
    for _hid, hname, ht, _hg in healthy:
        if hname not in clip_start or ht < clip_start[hname]:
            clip_start[hname] = ht
    ts_by_id = {rid: t for rid, t, _g in corrupted}
    for rid, cname in assigned.items():
        t = ts_by_id[rid]
        if cname not in clip_start or t < clip_start[cname]:
            clip_start[cname] = t

    with db_lock:
        for rid, cname in assigned.items():
            whereinclip = ts_by_id[rid] - clip_start[cname] + beforecliptime
            c.execute(
                "UPDATE videos SET name = ?, whereinclip = ? WHERE id = ?",
                (cname, whereinclip, rid),
            )
        conn.commit()
    conn.close()

    print(f"repair: healed {len(assigned)} corrupted clip-name row(s)")
    if orphans:
        print(f"repair: left {len(orphans)} orphan row(s) with no sibling clip untouched: {orphans}")


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
        # print("doing",file_path)
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
# print("eee")
# get_video_files()
# print("ooo")
def getjustnames():
    if not VIDEOS_DIR.exists():
        return []

    return list(map(lambda x: x.name ,VIDEOS_DIR.rglob("*"))) 

def get_video_files_names(index,requestcount):
    """Scan videos directory and return list of video files with metadata"""
    if not index: return []
    if not VIDEOS_DIR.exists():
        return []
    conn = sqlite3.connect(str(DB_PATH), timeout=10.0)
    c = conn.cursor()
    c.execute("SELECT MAX (id), MIN(timestamp) FROM videos")
    neatthings = c.fetchone()
    offset = not index + 1 and "0" or neatthings[0] -index
    c.execute(f"SELECT name, MIN(timestamp) AS timestamp, MIN(id) AS id FROM videos GROUP BY name ORDER BY id DESC LIMIT {requestcount} OFFSET {offset}")
    a = list(map(lambda x: {"url":x[0],"name":f"{x[0]}.mp4","timetaken":x[1],"id":x[2] },c.fetchall()))
    if not a:
        return list(filter(lambda x: int(x["timetaken"]) < float(neatthings[1]), map(lambda x: {"id":0,"url":x.name,"name":x.name,"timetaken":int(x.stat().st_mtime)} ,VIDEOS_DIR.rglob("*"))))
        
    return a

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
        ], check=True, capture_output=True, timeout=360)
        return result.stdout
    except Exception as e:
        try:
            result = subprocess.run([
                'ffmpeg',
                '-i', str(video_path),
                '-ss', '00:00:1.0',
                '-vframes', '1',
                '-vf', 'scale=320:-1',
                '-f', 'image2pipe',
                '-vcodec', 'mjpeg',
                'pipe:1'
            ], check=True, capture_output=True, timeout=360)
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
    print(videos[0])
    grouped_videos = group_videos_by_date(videos)
    # print("wooo")
    return render_template('index.html', grouped_videos=grouped_videos)

@app.route("/detail/<name>")
def detail(name):

    # print("meow",[name.rsplit(".",1)[0]])
    conn = sqlite3.connect(str(DB_PATH), timeout=10.0)
    c = conn.cursor()
    columns = [row[1] for row in c.execute("PRAGMA table_info(videos)")]
    # print(columns)
    try:
        c.execute(
            "SELECT * FROM videos WHERE name = ? ORDER BY gametime",
            (name.rsplit(".",1)[0],)
        )
        rows = c.fetchall()
    except sqlite3.OperationalError as e:
        # print(e)
        rows = []
    conn.close()
    # print(rows)
    if not rows:
        return {"data":None}


    returnstuff = list(map(lambda x: dict(zip(columns,x,strict = True)) ,rows))
    # print(returnstuff)
    return {"data":returnstuff}

@app.route('/videos')
def api_videos():
    """API endpoint returning videos grouped by date"""
    videos = get_video_files()
    print(videos[0])
    grouped_videos = group_videos_by_date(videos)

    result = []
    for date, vids in grouped_videos:
        result.append({
            'date': date,
            'videos': (vids)
        })

    return jsonify((result))


@app.route('/videonames',methods = ["POST"])
def api_videos_names():
    
    """API endpoint returning videos grouped by date"""
    return get_video_files_names(request.get_json()["mostrecentindex"],200)


@app.route('/durations')
def weeee():
    # time.sleep(5)
    return dict(map(lambda x: (x,f"{get_cached_duration(x,0) or 0:.2f}"), getjustnames()))
    
@app.route('/thumbnail/<path:video_path>')
def thumbnail(video_path):

    """Generate and serve thumbnail for video, caching it to THUMBS_DIR"""
    # Sanitize path
    # print(video_path)
    if '..' in video_path or video_path.startswith('/'):
        abort(403)

    video_file = VIDEOS_DIR / video_path
    if not video_file.exists():
        abort(404)
    makethumb(video_path)
    thumb_file = THUMBS_DIR / Path(video_path).with_suffix('.jpg')

    return send_file(thumb_file, mimetype='image/jpeg')

def makethumb(video_path):
    thumb_file = THUMBS_DIR / Path(video_path).with_suffix('.jpg')
    video_file = VIDEOS_DIR / video_path
    # Regenerate if missing or stale relative to the source video
    if not thumb_file.exists() or thumb_file.stat().st_mtime < video_file.stat().st_mtime:
        thumbnail_data = generate_thumbnail(video_file)
        if thumbnail_data is None:
            abort(500)

        thumb_file.parent.mkdir(parents=True, exist_ok=True)
        thumb_file.write_bytes(thumbnail_data)
 

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
    stuff = data["vars"] or request.get_json()["vars"]
    for thing,value in stuff.items():
        if thing not in columns:
            c.execute(f"ALTER TABLE videos ADD COLUMN {thing} {isinstance(value,int) and 'INTEGER' or 'TEXT'}")
    c.execute(f"INSERT INTO videos ({', '.join(list(stuff.keys()))}) VALUES ({', '.join(['?']*len(stuff))})",list(map(lambda x: x if not isinstance(x,dict) else json.dumps(x),stuff.values())))
    conn.commit()
    conn.close()
    return {"okay":"yes"}

def migrate_unwrap_json_columns_python():
    """One-time fix: rows written before dbinsert stopped json.dumps-ing every
    value. Re-normalises every existing cell to match the current rule
    (dicts stay json.dumps'd, everything else stored raw). Safe to re-run:
    already-migrated cells are no longer valid JSON text and get skipped."""
    conn = sqlite3.connect(str(DB_PATH), timeout=10.0)
    c = conn.cursor()
    columns = [row[1] for row in c.execute("PRAGMA table_info(videos)") if row[1] != "id"]
    if not columns:
        conn.close()
        return
    rows = c.execute(f"SELECT id, {', '.join(columns)} FROM videos").fetchall()
    for row_id, *values in rows:
        updates = {}
        for col, value in zip(columns, values):
            if value is None:
                continue
            try:
                parsed = json.loads(value)
            except (TypeError, json.JSONDecodeError):
                continue
            updates[col] = json.dumps(parsed) if isinstance(parsed, dict) else parsed
        if updates:
            set_clause = ", ".join(f"{col} = ?" for col in updates)
            c.execute(f"UPDATE videos SET {set_clause} WHERE id = ?", (*updates.values(), row_id))
    conn.commit()
    conn.close()

def migrate_unwrap_json_columns_sql():
    """Same one-time fix as migrate_unwrap_json_columns_python, but the
    unwrap/re-encode itself is done by SQLite's json_extract: for a JSON
    object it returns that object's own JSON text (a no-op re-encode), for
    any other JSON value it returns the raw unwrapped value. Requires
    SQLite's JSON1 functions (built in since SQLite 3.38)."""
    conn = sqlite3.connect(str(DB_PATH), timeout=10.0)
    c = conn.cursor()
    columns = [row[1] for row in c.execute("PRAGMA table_info(videos)") if row[1] != "id"]
    for col in columns:
        c.execute(
            f"UPDATE videos SET {col} = json_extract({col}, '$') "
            f"WHERE {col} IS NOT NULL AND json_valid({col})"
        )
    conn.commit()
    conn.close()

@app.route("/killlog", methods=["POST"])
def logakill():
    global clipdetails
    # print("Meow")
    # return {"moew":True}
    with histories_lock:
        clipdetails["clipno"] += 1
        clipno = clipdetails["clipno"]
    stuff = request.get_json()
    stuff["timestamp"] = correct_uploader_timestamp(float(stuff["timestamp"]))
    print("got a new kill!",stuff.get("attackername",False),"killed",stuff["victimname"])
    
    # print(json.dumps(stuff,indent=4))
    # query = sqlite3.connect("./database.db")
    # c = query.cursor()
    # columns = [row[1] for row in c.execute("PRAGMA table_info(videos)")]
    stuff["name"],stuff["whereinclip"]  = save_clip_buffer(stuff["timestamp"] - beforecliptime, stuff["timestamp"] + aftercliptime,f"{stuff['attackerweaponname']}_{stuff['localisattacker']}_{int(stuff['timestamp'])}_{clipno}",clipno,stuff["localisattacker"])
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

# migrate_unwrap_json_columns_sql()
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
    # Heal any rows the old save_clip_buffer death-race wrote with a bogus
    # name ("0") / unix-timestamp whereinclip before anything reads them.
    repair_corrupted_clip_names()
    # Recover any clip whose render was interrupted by a previous crash before
    # we start accepting new kills / recording new segments.
    threading.Thread(target=reconstruct_missing_clips, daemon=True).start()
    threading.Thread(target=main,daemon=True).start()
    print("Starting Waitress server on http://0.0.0.0:5000")
    serve(app, host='0.0.0.0', port=5000, threads=60,connection_limit=5000)
