"""
xLights Mashup Builder - Flask Web Application
===============================================
Provides an HTML interface for selecting sequences, configuring clip
time ranges, and building mashup sequences with merged effects and audio.
"""

import os
import sys
import json
import time
import traceback
import threading
import uuid
import io
import zipfile
from flask import Flask, render_template, jsonify, request, send_file

from core.mashup_engine import scan_sequences, build_mashup
from core import automix, library

app = Flask(__name__)

# ─── Configuration ──────────────────────────────────────────────────────
# Show paths live in config.json next to this file (kept out of git).
# Copy config.example.json to config.json and edit it. Environment
# variables XLMASHUP_SHOW_DIR / XLMASHUP_MEDIA_DIR override the file.
APP_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(APP_DIR, 'config.json')


def _load_config():
    cfg = {}
    try:
        with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
            cfg = json.load(f)
    except FileNotFoundError:
        pass
    except ValueError as e:
        print(f"⚠ config.json is not valid JSON ({e}) — ignoring it")
    show = os.environ.get('XLMASHUP_SHOW_DIR') or cfg.get('show_dir') or ''
    media = os.environ.get('XLMASHUP_MEDIA_DIR') or cfg.get('media_dir') or ''
    seqs = cfg.get('sequences_dir') or (os.path.join(show, 'Sequences') if show else '')
    return show, seqs, media


SHOW_DIR, SEQ_DIR, MEDIA_DIR = _load_config()


def _ask_folders_gui():
    """Folder pickers for first-run setup. Returns (show, media), None if
    cancelled, or raises if no GUI is available."""
    import tkinter as tk
    from tkinter import filedialog, messagebox
    root = tk.Tk()
    root.withdraw()
    root.attributes('-topmost', True)
    try:
        messagebox.showinfo(
            'xLights Mashup Builder — first-time setup',
            "Let's find your show.\n\n"
            "1. Choose your xLights show folder (the one that contains 'Sequences').\n"
            "2. Choose the folder with your music and video files.\n\n"
            "This is saved in config.json — you'll only be asked once.",
            parent=root)
        while True:
            show = filedialog.askdirectory(
                parent=root, mustexist=True,
                title="Choose your xLights show folder (contains 'Sequences')")
            if not show:
                return None
            if os.path.isdir(os.path.join(show, 'Sequences')):
                break
            if messagebox.askyesno(
                    'No Sequences folder',
                    f"{show}\n\ndoesn't contain a 'Sequences' folder.\n"
                    "Use it anyway?", parent=root):
                break
        media = filedialog.askdirectory(
            parent=root, mustexist=True, initialdir=os.path.dirname(show),
            title='Choose your media folder (music & videos) — Cancel to use the show folder')
        return os.path.normpath(show), os.path.normpath(media or show)
    finally:
        root.destroy()


def _ask_folders_console():
    """Fallback when no window can be shown."""
    print("First-time setup — paste folder paths (or press Enter to skip).")
    show = input("  Your xLights show folder (contains 'Sequences'): ").strip().strip('"')
    if not show:
        return None
    media = input("  Your media folder (music & videos) [same as show]: ").strip().strip('"')
    return os.path.normpath(show), os.path.normpath(media or show)


def ensure_config():
    """
    On launch: if config.json is missing (or its show folder no longer
    exists), ask for the folders and write config.json. Environment
    variables, when set, skip this.
    """
    global SHOW_DIR, SEQ_DIR, MEDIA_DIR
    if os.environ.get('XLMASHUP_SHOW_DIR'):
        return
    if os.path.exists(CONFIG_PATH) and SHOW_DIR and os.path.isdir(SHOW_DIR):
        return
    if os.path.exists(CONFIG_PATH):
        print(f"⚠ The show folder in config.json wasn't found: {SHOW_DIR or '(empty)'}")
    try:
        picked = _ask_folders_gui()
    except Exception as e:                      # no display / no tkinter
        print(f"  (Folder picker unavailable: {e})")
        try:
            picked = _ask_folders_console()
        except EOFError:
            picked = None
    if not picked:
        print("  Setup skipped — " + CONFIG_HELP)
        return
    show, media = picked
    cfg = {}
    try:
        with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
            cfg = json.load(f)
    except (OSError, ValueError):
        pass
    cfg.update({'show_dir': show, 'media_dir': media})
    cfg.pop('sequences_dir', None)
    with open(CONFIG_PATH, 'w', encoding='utf-8') as f:
        json.dump(cfg, f, indent=2)
    SHOW_DIR, SEQ_DIR, MEDIA_DIR = _load_config()
    print(f"✓ Saved your folders to config.json")
OUTPUT_DIR = os.path.join(APP_DIR, 'output')
SAVES_DIR = os.path.join(APP_DIR, 'saved_mashups')
ANALYSIS_CACHE_DIR = os.path.join(APP_DIR, 'analysis_cache')

CONFIG_HELP = ("Show folder not set up. Copy config.example.json to config.json "
               "next to app.py, set \"show_dir\" (your xLights show folder) and "
               "\"media_dir\" (your music/video folder), then restart the app.")


def safe_filename(name):
    """Sanitize a user-supplied name for use as a filename."""
    return "".join(c for c in name if c.isalnum() or c in ' -_').strip()

# Cache for sequence list (scanned once)
_sequences_cache = None
_scan_errors = []


def get_sequences():
    """Scan and cache the sequence list."""
    global _sequences_cache, _scan_errors
    if _sequences_cache is None:
        if not SHOW_DIR:
            raise RuntimeError(CONFIG_HELP)
        if not os.path.isdir(SEQ_DIR):
            raise RuntimeError(f"Sequences folder not found: {SEQ_DIR} — "
                               f"check show_dir in config.json")
        print(f"Scanning sequences in {SEQ_DIR}...")
        print(f"  Media dir: {MEDIA_DIR}")
        _sequences_cache = scan_sequences(
            SEQ_DIR, media_dir=MEDIA_DIR, show_dir=SHOW_DIR)
        with_media = sum(1 for s in _sequences_cache
                         if os.path.exists(s.media_file))
        # lets the analyser spot lyric tracks copied between sequences
        automix.set_sequence_paths([s.path for s in _sequences_cache])
        print(f"Found {len(_sequences_cache)} sequences "
              f"({with_media} with resolved media).")
    return _sequences_cache


# ─── Routes ─────────────────────────────────────────────────────────────

@app.route('/')
def index():
    """Serve the main HTML interface."""
    return render_template('index.html')


@app.route('/help')
def help_page():
    """User guide with screenshots (images in static/help/)."""
    return render_template('help.html')


@app.route('/api/sequences')
def api_sequences():
    """Return list of all available sequences."""
    try:
        seqs = get_sequences()
        return jsonify({
            'sequences': [s.to_dict() for s in seqs],
            'show_dir': SHOW_DIR,
            'seq_dir': SEQ_DIR,
        })
    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500


@app.route('/api/build_mashup', methods=['POST'])
def api_build_mashup():
    """Build a mashup from the provided clip configuration."""
    try:
        data = request.json
        clips = data.get('clips', [])
        mashup_name = data.get('name', 'mashup').strip()
        crossfade_ms = int(data.get('crossfade_ms', 0))

        if not clips:
            return jsonify({'error': 'No clips provided'}), 400

        if not mashup_name:
            mashup_name = f'mashup_{int(time.time())}'

        # Sanitize name for filesystem
        safe_name = "".join(
            c for c in mashup_name if c.isalnum() or c in ' -_'
        ).strip()
        if not safe_name:
            safe_name = 'mashup'

        by_path = {s.path: s for s in get_sequences()}
        clips_config = []
        for clip in clips:
            seq = by_path.get(clip['xsq_path'])
            clips_config.append({
                'xsq_path': clip['xsq_path'],
                'media_file': seq.media_file if seq else None,
                'start_ms': int(clip['start_ms']),
                'end_ms': int(clip['end_ms']),
                'crossfade_ms': crossfade_ms,
            })

        output_subdir = os.path.join(OUTPUT_DIR, safe_name)

        result = build_mashup(clips_config, output_subdir, safe_name,
                              normalize_audio=bool(data.get('normalize_audio', True)),
                              fade_out_ms=max(0, min(30000, int(data.get('fade_out_ms') or 0))))

        # Return paths relative to OUTPUT_DIR for download
        result['download_xsq'] = f'{safe_name}/{safe_name}.xsq'
        result['download_audio'] = f'{safe_name}/{safe_name}.mp3'
        result['download_zip'] = safe_name
        # path relative to the app folder (no user/drive details in the UI)
        result['xsq_rel'] = os.path.relpath(result['xsq_path'], APP_DIR)

        return jsonify({
            'success': True,
            'result': result,
        })

    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500


# ─── Saved Mashup Projects ──────────────────────────────────────────────

def _save_path(file_id):
    """Resolve a saved-project id to a path inside SAVES_DIR (or None)."""
    safe = safe_filename(file_id)
    if not safe or safe != file_id:
        return None
    return os.path.join(SAVES_DIR, safe + '.json')


@app.route('/api/mashups', methods=['GET'])
def api_list_mashups():
    """List saved mashup projects, newest first."""
    os.makedirs(SAVES_DIR, exist_ok=True)
    items = []
    for fn in os.listdir(SAVES_DIR):
        if not fn.lower().endswith('.json'):
            continue
        try:
            with open(os.path.join(SAVES_DIR, fn), 'r', encoding='utf-8') as f:
                proj = json.load(f)
            clips = proj.get('clips', [])
            items.append({
                'id': fn[:-5],
                'name': proj.get('name', fn[:-5]),
                'saved_at': proj.get('saved_at', 0),
                'num_clips': len(clips),
                'duration_ms': sum(max(0, c.get('end_ms', 0) - c.get('start_ms', 0))
                                   for c in clips),
                'clip_names': [c.get('seq_name', '') for c in clips],
            })
        except Exception as e:
            print(f"Skipping unreadable save {fn}: {e}")
    items.sort(key=lambda x: x['saved_at'], reverse=True)
    return jsonify({'mashups': items})


@app.route('/api/mashups/<file_id>', methods=['GET'])
def api_get_mashup(file_id):
    """Return one saved mashup project."""
    path = _save_path(file_id)
    if not path or not os.path.exists(path):
        return jsonify({'error': 'Saved mashup not found'}), 404
    with open(path, 'r', encoding='utf-8') as f:
        return jsonify({'mashup': json.load(f)})


@app.route('/api/mashups', methods=['POST'])
def api_save_mashup():
    """Save a mashup project. Returns 409 if it exists and overwrite is false."""
    try:
        data = request.json or {}
        name = (data.get('name') or '').strip()
        file_id = safe_filename(name)
        if not file_id:
            return jsonify({'error': 'Please enter a mashup name '
                                     '(letters, numbers, spaces, - or _)'}), 400

        path = os.path.join(SAVES_DIR, file_id + '.json')
        if os.path.exists(path) and not data.get('overwrite'):
            return jsonify({'error': 'exists', 'id': file_id}), 409

        project = {
            'version': 1,
            'name': name,
            'saved_at': time.time(),
            'crossfade_ms': int(data.get('crossfade_ms', 0)),
            'max_length_ms': (int(data['max_length_ms'])
                              if data.get('max_length_ms') else None),
            'clips': [{
                'xsq_path': c['xsq_path'],
                'seq_name': c.get('seq_name', ''),
                'start_ms': int(c['start_ms']),
                'end_ms': int(c['end_ms']),
                'note': str(c.get('note', ''))[:300],
                'origin': 'generated' if c.get('origin') == 'generated' else 'user',
            } for c in data.get('clips', [])],
            'excluded': [str(p) for p in data.get('excluded', [])][:500],
            'normalize_audio': bool(data.get('normalize_audio', True)),
            'fade_out_ms': max(0, min(30000, int(data.get('fade_out_ms') or 0))),
        }

        os.makedirs(SAVES_DIR, exist_ok=True)
        tmp = path + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(project, f, indent=2)
        os.replace(tmp, path)   # atomic, so a crash can't leave a half-written save

        return jsonify({'success': True, 'id': file_id})
    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500


@app.route('/api/mashups/<file_id>', methods=['DELETE'])
def api_delete_mashup(file_id):
    """Delete a saved mashup project."""
    path = _save_path(file_id)
    if not path or not os.path.exists(path):
        return jsonify({'error': 'Saved mashup not found'}), 404
    os.remove(path)
    return jsonify({'success': True})


# ─── Automate (auto-mix planner) ────────────────────────────────────────

_automate_jobs = {}
_automate_lock = threading.Lock()


def _run_automate(job_id, seqs, opts):
    job = _automate_jobs[job_id]

    def progress(frac, msg):
        job['progress'] = round(frac, 3)
        job['message'] = msg

    try:
        job['result'] = automix.plan_mashup(
            seqs, ANALYSIS_CACHE_DIR,
            target_s=opts['target_s'],
            allow_reorder=opts['allow_reorder'],
            allow_reuse=opts['allow_reuse'],
            max_total_s=opts['max_total_s'],
            online=opts['online'],
            crossfade_ms=opts['crossfade_ms'],
            order_mode=opts['order_mode'],
            progress=progress)
        job['status'] = 'done'
    except Exception as e:
        traceback.print_exc()
        job['error'] = str(e)
        job['status'] = 'error'


@app.route('/api/automate', methods=['POST'])
def api_automate():
    """Start an auto-mix job for the clips currently in the timeline."""
    data = request.json or {}
    paths = [c.get('xsq_path') for c in data.get('clips', [])]
    if not paths:
        return jsonify({'error': 'Add some sequences to the timeline first.'}), 400

    by_path = {s.path: s for s in get_sequences()}
    seqs = []
    for p in paths:
        s = by_path.get(p)
        if s is None or not os.path.exists(s.media_file):
            return jsonify({'error': f'Unknown sequence or missing media: {p}'}), 400
        seqs.append(s.to_dict())

    xf = data.get('crossfade_ms')
    opts = {
        'target_s': max(8, min(180, float(data.get('target_s', 30)))),
        'allow_reorder': bool(data.get('allow_reorder', True)),
        'allow_reuse': bool(data.get('allow_reuse', True)),
        'max_total_s': (max(10, float(data['max_total_s']))
                        if data.get('max_total_s') else None),
        'online': bool(data.get('online', True)),
        'crossfade_ms': None if xf is None else max(0, int(xf)),
        'order_mode': (data.get('order_mode')
                       if data.get('order_mode') in ('best', 'scramble', 'keep') else None),
    }

    job_id = uuid.uuid4().hex
    with _automate_lock:
        # forget old finished jobs
        for k in [k for k, j in _automate_jobs.items()
                  if j['status'] != 'running' and time.time() - j['started'] > 3600]:
            del _automate_jobs[k]
        _automate_jobs[job_id] = {'status': 'running', 'progress': 0,
                                  'message': 'Starting…', 'result': None,
                                  'error': None, 'started': time.time()}
    threading.Thread(target=_run_automate, args=(job_id, seqs, opts),
                     daemon=True).start()
    return jsonify({'job_id': job_id})


@app.route('/api/automate/<job_id>')
def api_automate_status(job_id):
    job = _automate_jobs.get(job_id)
    if not job:
        return jsonify({'error': 'Job not found'}), 404
    return jsonify({k: job[k] for k in ('status', 'progress', 'message',
                                        'result', 'error')})


# ─── Song library: categories + generator ───────────────────────────────

_library_store = library.Store(os.path.dirname(os.path.abspath(__file__)))
_library_busy = threading.Lock()      # one scan / generate at a time


def _start_job(work):
    """Run work(progress) in a thread; poll via /api/jobs/<id>."""
    job_id = uuid.uuid4().hex
    job = {'status': 'running', 'progress': 0, 'message': 'Starting…',
           'result': None, 'error': None, 'started': time.time()}
    with _automate_lock:
        _automate_jobs[job_id] = job

    def progress(frac, msg):
        job['progress'] = round(frac, 3)
        job['message'] = msg

    def run():
        with _library_busy:
            try:
                job['result'] = work(progress)
                job['status'] = 'done'
            except Exception as e:
                traceback.print_exc()
                job['error'] = str(e)
                job['status'] = 'error'

    threading.Thread(target=run, daemon=True).start()
    return job_id


def _playable_seqs():
    return {s.path: s.to_dict() for s in get_sequences()
            if os.path.exists(s.media_file)}


@app.route('/api/jobs/<job_id>')
def api_job_status(job_id):
    return api_automate_status(job_id)


@app.route('/api/library')
def api_library():
    return jsonify(library.view(_library_store))


@app.route('/api/library/scan', methods=['POST'])
def api_library_scan():
    data = request.json or {}
    online = bool(data.get('online', True))
    seqs = list(_playable_seqs().values())

    def work(progress):
        library.scan(seqs, _library_store, ANALYSIS_CACHE_DIR, online=online,
                     progress=progress)
        return library.view(_library_store)
    return jsonify({'job_id': _start_job(work)})


@app.route('/api/library/categories', methods=['POST'])
def api_library_categories():
    data = request.json or {}
    path = data.get('path')
    if path not in _playable_seqs():
        return jsonify({'error': 'Unknown sequence'}), 400
    library.set_categories(_library_store, path, data.get('categories', []))
    return jsonify(library.view(_library_store))


@app.route('/api/generate', methods=['POST'])
def api_generate():
    data = request.json or {}
    seq_by_path = _playable_seqs()
    xf = data.get('crossfade_ms')
    opts = {
        'categories': [str(c) for c in data.get('categories', [])],
        'match_all': bool(data.get('match_all', True)),
        'count': max(1, min(20, int(data.get('count', 6)))),
        'pinned': [p for p in data.get('pinned', []) if p in seq_by_path],
        'excluded': [p for p in data.get('excluded', []) if p in seq_by_path],
        'target_s': max(8, min(180, float(data.get('target_s', 30)))),
        'max_total_s': (max(10, float(data['max_total_s']))
                        if data.get('max_total_s') else None),
        'allow_reuse': bool(data.get('allow_reuse', True)),
        'online': bool(data.get('online', True)),
        'crossfade_ms': None if xf is None else max(0, int(xf)),
    }

    def work(progress):
        return library.generate(_library_store, seq_by_path, ANALYSIS_CACHE_DIR,
                                opts, progress=progress)
    return jsonify({'job_id': _start_job(work)})


@app.route('/api/download_all/<name>')
def api_download_all(name):
    """Download a built mashup (.xsq + .mp3) as one zip, inside a folder of
    the mashup's name so it can be dropped straight into the Sequences dir."""
    if not name or safe_filename(name) != name:
        return jsonify({'error': 'Invalid mashup name'}), 400
    folder = os.path.join(OUTPUT_DIR, name)
    if not os.path.isdir(folder):
        return jsonify({'error': 'Mashup not found — build it first'}), 404

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w', zipfile.ZIP_DEFLATED) as zf:
        for fn in sorted(os.listdir(folder)):
            fp = os.path.join(folder, fn)
            if os.path.isfile(fp) and not fn.startswith('_segment_'):
                # mp3 is already compressed; storing it is faster
                ctype = zipfile.ZIP_STORED if fn.lower().endswith('.mp3') else zipfile.ZIP_DEFLATED
                zf.write(fp, arcname=f'{name}/{fn}', compress_type=ctype)
    buf.seek(0)
    return send_file(buf, mimetype='application/zip', as_attachment=True,
                     download_name=f'{name}.zip')


@app.route('/api/download/<path:filename>')
def api_download(filename):
    """Download a generated mashup file."""
    filepath = os.path.join(OUTPUT_DIR, filename)
    if os.path.exists(filepath):
        return send_file(filepath, as_attachment=True)
    return jsonify({'error': 'File not found'}), 404


# ─── Main ───────────────────────────────────────────────────────────────

PORT = 5000
APP_URL = f'http://localhost:{PORT}'


def _port_in_use(port):
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex(('127.0.0.1', port)) == 0


def _open_browser_when_ready(url, port, timeout_s=30):
    """Open the default browser as soon as the server accepts connections."""
    import webbrowser

    def wait_and_open():
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            if _port_in_use(port):
                webbrowser.open(url)
                return
            time.sleep(0.25)
        print(f"  (Server took too long to start — open {url} yourself.)")

    threading.Thread(target=wait_and_open, daemon=True).start()


if __name__ == '__main__':
    open_browser = '--no-browser' not in sys.argv

    # Already running (e.g. launched twice)? Just show it instead of failing.
    if _port_in_use(PORT):
        print(f"xLights Mashup Builder is already running at {APP_URL}")
        if open_browser:
            import webbrowser
            webbrowser.open(APP_URL)
        sys.exit(0)

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    os.makedirs(SAVES_DIR, exist_ok=True)
    ensure_config()          # first launch: pick folders, write config.json
    if not SHOW_DIR or not os.path.isdir(SEQ_DIR):
        print("⚠ " + (CONFIG_HELP if not SHOW_DIR
                      else f"Sequences folder not found: {SEQ_DIR} (check config.json)"))
        print()
    print(f"xLights Mashup Builder")
    print(f"  Saves Dir:  {SAVES_DIR}")
    print(f"  Show Dir:   {SHOW_DIR}")
    print(f"  Sequences:  {SEQ_DIR}")
    print(f"  Output Dir: {OUTPUT_DIR}")
    print(f"  Starting server on {APP_URL}"
          + ("  (opening your browser…)" if open_browser else ""))
    print()
    if open_browser:
        _open_browser_when_ready(APP_URL, PORT)
    app.run(debug=False, port=PORT, host='0.0.0.0')
