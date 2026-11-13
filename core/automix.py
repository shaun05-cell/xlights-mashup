"""
Auto-Mix Planner
================
Listens to each song in a mashup and picks musically sensible clip ranges
and a running order.

Signals used (best available source wins):

  Lyrics   1. The sequence's own lyric timing track (aligned to *this* mp3)
           2. LRCLIB synced lyrics (online, duration-matched)
           3. None -> treated as instrumental, structure from audio
  Beats    1. Sequence "Beats"/"Beat Count" timing track, "Bars" for downbeats
           2. Audio beat tracking (spectral-flux onset + autocorrelation)
  Audio    Loudness curve, musical key (Krumhansl), section novelty

Planning:
  * Lyric lines are grouped into blocks (verse / chorus). Lines that repeat
    elsewhere in the song mark the chorus; title words mark the hook.
  * Clip starts snap to the downbeat just before a block's first line,
    clip ends snap to the first downbeat after a block's last line, and
    any cut that would chop a sung phrase is penalised.
  * Candidate clips are scored (contains a chorus, ends on a chorus,
    energy, closeness to the target length, clean cuts).
  * Running order + clip choice is optimised jointly (exact DP for up to 9
    songs) using tempo match, Camelot key compatibility and loudness flow.
  * With a crossfade, cuts are shifted by half the fade so the fade is
    centred on the bar line, keeping both songs' downbeats aligned.
"""

import os
import re
import json
import math
import html
import time
import bisect
import random
import hashlib
import subprocess
import urllib.request
import urllib.parse
import xml.etree.ElementTree as ET

import numpy as np
import imageio_ffmpeg

CACHE_VERSION = 9
SR = 11025
USER_AGENT = 'xLights-Mashup-Builder/1.0'

_NOTE_NAMES = ['C', 'C#', 'D', 'Eb', 'E', 'F', 'F#', 'G', 'Ab', 'A', 'Bb', 'B']
_MAJOR_PROFILE = np.array([6.35, 2.23, 3.48, 2.33, 4.38, 4.09,
                           2.52, 5.19, 2.39, 3.66, 2.29, 2.88])
_MINOR_PROFILE = np.array([6.33, 2.68, 3.52, 5.38, 2.60, 3.53,
                           2.54, 4.75, 3.98, 2.69, 3.34, 3.17])


# ═══════════════════════════════════════════════════════════════════════
#  Audio analysis
# ═══════════════════════════════════════════════════════════════════════

def decode_audio(path, sr=SR):
    """Decode any audio/video file to mono float32 via ffmpeg."""
    cmd = [imageio_ffmpeg.get_ffmpeg_exe(), '-v', 'error', '-i', path,
           '-vn', '-ac', '1', '-ar', str(sr), '-f', 's16le', '-']
    flags = getattr(subprocess, 'CREATE_NO_WINDOW', 0)
    out = subprocess.run(cmd, capture_output=True, check=True,
                         creationflags=flags).stdout
    return np.frombuffer(out, dtype=np.int16).astype(np.float32) / 32768.0


def _frames(y, n_fft, hop):
    if len(y) < n_fft:
        y = np.pad(y, (0, n_fft - len(y)))
    y = np.ascontiguousarray(y)
    n = 1 + (len(y) - n_fft) // hop
    return np.lib.stride_tricks.as_strided(
        y, shape=(n, n_fft), strides=(y.strides[0] * hop, y.strides[0]))


def _stft_mag(y, n_fft, hop):
    fr = _frames(y, n_fft, hop)
    win = np.hanning(n_fft).astype(np.float32)
    out = np.empty((fr.shape[0], n_fft // 2 + 1), np.float32)
    for i in range(0, fr.shape[0], 2048):
        out[i:i + 2048] = np.abs(np.fft.rfft(fr[i:i + 2048] * win, axis=1))
    return out


def _energy_db(y):
    """Loudness in dBFS per 100 ms, lightly smoothed (≈1 s)."""
    hop = SR // 10
    fr = _frames(y, hop, hop)
    rms = np.sqrt(np.mean(fr.astype(np.float64) ** 2, axis=1) + 1e-12)
    k = np.ones(9) / 9
    rms = np.convolve(rms, k, mode='same')
    return 20 * np.log10(rms + 1e-6)


def _onset_envelopes(y):
    """Spectral-flux onset strength (full band and low band) at ~43 fps."""
    n_fft, hop = 1024, 256
    S = np.log1p(100 * _stft_mag(y, n_fft, hop))
    d = np.maximum(0, np.diff(S, axis=0))
    low_bins = int(150 / (SR / n_fft)) + 1
    full = np.concatenate([[0], d.sum(axis=1)])
    low = np.concatenate([[0], d[:, :low_bins].sum(axis=1)])
    fps = SR / hop
    # remove slow trend so autocorrelation sees pulses, not loudness
    w = max(1, int(fps))
    full = np.maximum(0, full - np.convolve(full, np.ones(w) / w, mode='same'))
    low = np.maximum(0, low - np.convolve(low, np.ones(w) / w, mode='same'))
    return full, low, fps


def _track_beats(onset, fps):
    """Constant-tempo beat grid: tempo by autocorrelation, then phase search."""
    n = len(onset)
    if n < fps * 8 or onset.max() <= 0:
        return 120.0, []
    f = np.fft.rfft(onset, 2 * n)
    ac = np.fft.irfft(f * np.conj(f))[:n]
    min_lag, max_lag = int(fps * 60 / 200), int(fps * 60 / 60)
    lags = np.arange(min_lag, min(max_lag, n - 1) + 1)
    bpm = 60 * fps / lags
    prior = np.exp(-0.5 * (np.log2(bpm / 115) / 0.9) ** 2)
    lag = lags[int(np.argmax(ac[lags] * prior))]
    base_bpm = 60 * fps / lag

    best = (-1, base_bpm, 0.0)
    for cand in np.linspace(base_bpm * 0.985, base_bpm * 1.015, 31):
        period = 60 * fps / cand
        for phi in np.arange(0, period, 0.5):
            idx = np.round(np.arange(phi, n, period)).astype(int)
            idx = idx[idx < n]
            s = onset[idx].mean()
            if s > best[0]:
                best = (s, cand, phi)
    _, tempo, phi = best
    period = 60 * fps / tempo
    beats = [float(t / fps * 1000) for t in np.arange(phi, n, period)]
    return float(tempo), beats


def _choose_downbeats(beats_ms, low_onset, fps, beats_per_bar=4):
    """Pick which beat in each bar is the downbeat (strongest low end)."""
    if len(beats_ms) < beats_per_bar * 2:
        return list(beats_ms[::beats_per_bar])
    idx = np.clip((np.array(beats_ms) / 1000 * fps).astype(int), 0,
                  len(low_onset) - 1)
    strength = low_onset[idx]
    scores = [strength[p::beats_per_bar].mean() for p in range(beats_per_bar)]
    p = int(np.argmax(scores))
    return list(beats_ms[p::beats_per_bar])


def _timbre_features(y, onset, fps, energy_db):
    """Bass weight, brightness, beat density, dynamics: used for song picking."""
    n_fft = 2048
    S = _stft_mag(y, n_fft, 1024)
    P = (S.astype(np.float64) ** 2).mean(axis=0)
    freqs = np.fft.rfftfreq(n_fft, 1 / SR)
    band = freqs < 5000
    tot = P[band].sum() + 1e-12
    bass = float(P[freqs < 150].sum() / tot)
    centroid = float((P[band] * freqs[band]).sum() / tot)
    # onset peaks per second (how busy / percussive the track is)
    o = onset / (onset.max() + 1e-9)
    peaks = (o[1:-1] > o[:-2]) & (o[1:-1] >= o[2:]) & (o[1:-1] > 0.15)
    density = float(peaks.sum() / (len(o) / fps + 1e-9))
    voiced = energy_db[energy_db > energy_db.max() - 40]
    dyn = float(np.percentile(voiced, 95) - np.percentile(voiced, 10)) if len(voiced) else 0.0
    loud = float(np.median(voiced)) if len(voiced) else -60.0
    return {'bass_ratio': round(bass, 4), 'brightness_hz': round(centroid),
            'onset_density': round(density, 2), 'dynamics_db': round(dyn, 1),
            'loudness_db': round(loud, 1)}


def _chroma_frames(y):
    """12-bin chroma per ~186 ms frame."""
    n_fft, hop = 4096, 2048
    S = _stft_mag(y, n_fft, hop)
    freqs = np.fft.rfftfreq(n_fft, 1 / SR)
    mask = (freqs >= 55) & (freqs <= 2000)
    midi = 69 + 12 * np.log2(freqs[mask] / 440.0)
    pc = np.round(midi).astype(int) % 12
    M = np.zeros((mask.sum(), 12), np.float32)
    M[np.arange(mask.sum()), pc] = 1
    chroma = np.sqrt(S[:, mask]) @ M
    times = (np.arange(chroma.shape[0]) * hop + n_fft / 2) / SR * 1000
    return chroma, times


def _estimate_key(chroma):
    prof = chroma.sum(axis=0)
    if prof.sum() <= 0:
        return None
    prof = prof / prof.sum()
    best = (-2, 0, 'major')
    for k in range(12):
        for mode, P in (('major', _MAJOR_PROFILE), ('minor', _MINOR_PROFILE)):
            r = np.corrcoef(prof, np.roll(P, k))[0, 1]
            if r > best[0]:
                best = (r, k, mode)
    _, pc, mode = best
    return {'pc': int(pc), 'mode': mode, 'name': f"{_NOTE_NAMES[pc]} {mode}",
            'camelot': camelot(pc, mode)}


def camelot(pc, mode):
    if mode == 'minor':
        pc = (pc + 3) % 12          # relative major shares the number
    num = ((pc * 7) % 12 + 7) % 12 + 1
    return f"{num}{'A' if mode == 'minor' else 'B'}"


def _audio_boundaries(chroma, ctimes, energy_db, downbeats, duration_ms):
    """Section-change points (ms) from bar-level chroma + loudness novelty."""
    db = np.array(downbeats)
    if len(db) < 16:
        return []
    feats = []
    for a, b in zip(db[:-1], db[1:]):
        m = (ctimes >= a) & (ctimes < b)
        c = chroma[m].mean(axis=0) if m.any() else np.zeros(12)
        c = c / (np.linalg.norm(c) + 1e-9)
        e = energy_db[int(a / 100):max(int(a / 100) + 1, int(b / 100))]
        feats.append(np.concatenate([c, [np.mean(e) / 20.0 if len(e) else -3]]))
    F = np.array(feats)
    F[:, -1] = (F[:, -1] - F[:, -1].mean()) / (F[:, -1].std() + 1e-9) * 0.35
    W = 4
    nov = np.zeros(len(F))
    for i in range(W, len(F) - W):
        a, b = F[i - W:i].mean(axis=0), F[i:i + W].mean(axis=0)
        nov[i] = np.linalg.norm(a - b)
    picks = []
    for i in np.argsort(-nov):
        if nov[i] <= 0:
            break
        if all(abs(i - p) >= 8 for p in picks):
            picks.append(int(i))
        if len(picks) >= max(3, int(duration_ms / 15000)):
            break
    return sorted(float(db[i]) for i in picks)


# ═══════════════════════════════════════════════════════════════════════
#  Sequence timing tracks (lyrics, beats, bars)
# ═══════════════════════════════════════════════════════════════════════

def _read_timing_tracks(xsq_path):
    root = ET.parse(xsq_path).getroot()
    head = root.find('head')
    meta = {
        'song': html.unescape(html.unescape(head.findtext('song', '') or '')).strip(),
        'artist': html.unescape(html.unescape(head.findtext('artist', '') or '')).strip(),
    }
    tracks = []
    for el in root.findall('./ElementEffects/Element[@type="timing"]'):
        layers = []
        for L in el.findall('EffectLayer'):
            effs = []
            for e in L.findall('Effect'):
                try:
                    s, t = int(float(e.get('startTime'))), int(float(e.get('endTime')))
                except (TypeError, ValueError):
                    continue
                effs.append((s, t, (e.get('label') or '').strip()))
            effs.sort()
            layers.append(effs)
        tracks.append({'name': el.get('name', ''), 'layers': layers})
    return meta, tracks


def _has_letters(s):
    return bool(re.search(r'[A-Za-z]', s))


def _group_words(words, gap=650, max_len=9000):
    lines, cur = [], None
    for s, t, lab in words:
        if cur and (s - cur['end'] > gap or s - cur['start'] > max_len):
            lines.append(cur)
            cur = None
        if cur is None:
            cur = {'start': s, 'end': t, 'text': lab}
        else:
            cur['end'] = max(cur['end'], t)
            cur['text'] += ' ' + lab
    if cur:
        lines.append(cur)
    return lines


def _lyric_track_lines(layers):
    l0 = [e for e in layers[0] if e[2]] if layers else []
    l1 = [e for e in layers[1] if e[2]] if len(layers) > 1 else []
    words = l1 or l0
    usable0 = (len(l0) >= 4 and (not l1 or len(l0) <= 0.7 * len(l1))
               and np.median([e[1] - e[0] for e in l0]) <= 15000)
    if usable0:
        lines = [{'start': s, 'end': t, 'text': lab} for s, t, lab in l0]
    else:
        lines = _group_words(words)
    return lines, words


def _merge_intervals(iv, tol=150):
    iv = sorted(iv)
    out = []
    for s, t in iv:
        if out and s <= out[-1][1] + tol:
            out[-1][1] = max(out[-1][1], t)
        else:
            out.append([s, t])
    return out


_TITLE_STOP = {'the', 'a', 'an', 'of', 'is', 'you', 'i', 'me', 'my', 'to', 'in', 'on',
               'and', 'for', 'it', 'we', 'dont', "don't", 'be', 'this', 'that', 'hd'}
_COMMON = _TITLE_STOP | {
    'your', "you're", 'are', 'was', 'were', 'am', "i'm", 'im', 'so', 'but', 'or', 'not', 'no',
    'oh', 'ooh', 'yeah', 'hey', 'la', 'na', 'ah', 'all', 'just', 'now', 'can', "can't", 'with',
    'at', 'by', 'up', 'down', 'out', 'what', 'when', 'like', 'get', 'got', 'go', 'know', 'see',
    'he', 'she', 'they', 'them', 'his', 'her', 'our', 'us', 'there', 'here', 'do', 'one',
    'love', 'want', 'make', 'time', 'night', 'day', 'way', 'come', 'let', "it's", 'its', 'how'}


def _vocab(words):
    return {re.sub(r"[^a-z']", '', w.lower()) for _, _, lab in words
            for w in lab.split()} - {''}


# Fingerprints of every lyric track in the show, to spot tracks that were
# copied into a sequence from another song (e.g. via a shared template).
_SEQ_PATHS = []
_FP_INDEX = None


def set_sequence_paths(paths):
    """Tell the analyser about every sequence in the show (enables copy detection)."""
    global _SEQ_PATHS, _FP_INDEX
    paths = sorted(paths)
    if paths != _SEQ_PATHS:
        _SEQ_PATHS, _FP_INDEX = paths, None


def _track_fp(layers):
    words = [e for e in (layers[1] if len(layers) > 1 else layers[0]) if e[2]] or \
            [e for e in layers[0] if e[2]]
    key = '|'.join(f"{s}:{lab.lower()}" for s, _, lab in words[:80])
    return hashlib.sha1(key.encode('utf-8')).hexdigest() if key else None


def _fp_index():
    global _FP_INDEX
    if _FP_INDEX is None:
        idx = {}
        for p in _SEQ_PATHS:
            try:
                _, tracks = _read_timing_tracks(p)
            except Exception:
                continue
            for tr in tracks:
                if len(tr['layers']) >= 2:
                    fp = _track_fp(tr['layers'])
                    if fp:
                        idx.setdefault(fp, set()).add(p)
        _FP_INDEX = idx
    return _FP_INDEX


def _sequence_lyrics(tracks, title='', seq_path=None):
    """
    Return (lines, vocal_intervals, track_name, ignored_track_names).

    Sequences built from a template sometimes carry other songs' lyric
    tracks. A track is ignored when the identical track (same words at the
    same times) also exists in another sequence of the show and it never
    sings this song's title. Duet / backing parts unique to this sequence
    are always kept.
    """
    lyric = []
    for tr in tracks:
        L = tr['layers']
        labeled = sum(1 for layer in L for e in layer if _has_letters(e[2]))
        if len(L) >= 2 and labeled >= 10:
            lines, words = _lyric_track_lines(L)
            if lines:
                lyric.append((len(words), tr['name'], lines, words, _track_fp(L)))
    if not lyric:
        return [], [], None, []

    t_toks = set(re.sub(r"[^a-z' ]", ' ', title.lower()).split()) - _TITLE_STOP
    hit = {x[1]: (len(t_toks & _vocab(x[3])) / len(t_toks) if t_toks else 0)
           for x in lyric}

    ignored = []
    if seq_path and _SEQ_PATHS:
        idx = _fp_index()
        shared = {x[1]: idx.get(x[4], set()) - {seq_path} for x in lyric}
        own_sets = [shared[x[1]] for x in lyric if hit[x[1]] >= 0.5]
        for _, name, _, _, fp in lyric:
            if not shared[name] or hit[name] >= 0.5:
                continue
            # copied out *together with* this song's own title track -> ours
            if any(shared[name] <= s for s in own_sets):
                continue
            ignored.append(name)
        if len(ignored) < len(lyric):
            lyric = [x for x in lyric if x[1] not in ignored]
        else:
            ignored = []                    # never throw away everything

    # Main lyrics: the biggest track that sings the title (if it's a real part)
    biggest = max(x[0] for x in lyric)
    owners = {x[1] for x in lyric if hit[x[1]] >= 0.5 and x[0] >= 0.3 * biggest}
    lyric.sort(key=lambda x: (x[1] not in owners, -x[0]))
    _, name, lines, _, _ = lyric[0]
    vocals = _merge_intervals(
        [(s, t) for _, _, _, words, _ in lyric for s, t, _ in words if t > s])
    return lines, vocals, name, ignored


def _interval_stats(times):
    if len(times) < 8:
        return None
    d = np.diff(times)
    return float(np.median(d)), float(times[-1] - times[0])


def _sequence_beats(tracks, duration_ms):
    """Beats/downbeats from timing tracks, or (None, None, None)."""
    beats = downbeats = None
    beat_name = None
    for tr in tracks:
        if len(tr['layers']) != 1:
            continue
        nm = tr['name'].lower()
        effs = tr['layers'][0]
        times = [e[0] for e in effs]
        st = _interval_stats(times)
        if not st:
            continue
        med, span = st
        if re.search(r'\bbars?\b', nm) and 1000 <= med <= 5000 and span > 0.5 * duration_ms:
            downbeats = downbeats or [float(t) for t in times]
        elif ('beat' in nm or 'metronome' in nm) and 250 <= med <= 1200 \
                and span > 0.5 * duration_ms and beats is None:
            beats = [float(t) for t in times]
            beat_name = tr['name']
            labels = [e[2] for e in effs]
            if downbeats is None and all(l.isdigit() for l in labels):
                ones = [float(e[0]) for e in effs if e[2] == '1']
                if 2 <= len(beats) / max(1, len(ones)) <= 8:
                    downbeats = ones
    return beats, downbeats, beat_name


# ═══════════════════════════════════════════════════════════════════════
#  Online lyrics (LRCLIB)
# ═══════════════════════════════════════════════════════════════════════

def _clean_title(s):
    s = re.sub(r'^\(.*?\)\s*', '', s)                 # "(Comedy) ..."
    s = re.sub(r'\s*\((?:official|lyric|audio|hd|remaster|.*?version).*?\)', '', s, flags=re.I)
    s = re.sub(r'\b(HD|HQ|Lyrics?|Official|Audio)\b', '', s, flags=re.I)
    s = re.sub(r'^\d+\s*[-.]\s*', '', s)              # "03 - Song"
    return re.sub(r'\s+', ' ', s.replace('_', ' ')).strip(' -')


def _title_candidates(seq_name, media_file, meta):
    pairs = []
    name = _clean_title(seq_name)
    if ' - ' in name:
        a, b = [p.strip() for p in name.split(' - ', 1)]
        pairs += [(a, b), (b, a)]
    if meta.get('song'):
        pairs.append((_clean_title(meta['song']), meta.get('artist', '')))
    stem = _clean_title(os.path.splitext(os.path.basename(media_file))[0])
    pairs.append((name, ''))
    if stem.lower() != name.lower():
        pairs.append((stem, ''))
    seen, out = set(), []
    for t, a in pairs:
        k = (t.lower(), a.lower())
        if t and k not in seen:
            seen.add(k)
            out.append((t, a))
    return out


def _http_json(url):
    req = urllib.request.Request(url, headers={'User-Agent': USER_AGENT})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode('utf-8'))


def _parse_lrc(text):
    out = []
    for raw in text.splitlines():
        stamps = re.findall(r'\[(\d+):(\d+(?:\.\d+)?)\]', raw)
        lyric = re.sub(r'\[[^\]]*\]', '', raw).strip()
        for m, s in stamps:
            out.append((int((int(m) * 60 + float(s)) * 1000), lyric))
    out.sort()
    lines = []
    for i, (t, lyr) in enumerate(out):
        if not lyr:
            continue
        nxt = out[i + 1][0] if i + 1 < len(out) else t + 6000
        est = 900 + 380 * len(lyr.split())
        lines.append({'start': t, 'end': int(min(nxt - 100, t + est)), 'text': lyr})
    return lines


def fetch_online_lyrics(seq_name, media_file, meta, duration_ms, log=print):
    """Search LRCLIB for duration-matched synced lyrics."""
    dur_s = duration_ms / 1000
    for title, artist in _title_candidates(seq_name, media_file, meta)[:5]:
        params = {'track_name': title}
        if artist:
            params['artist_name'] = artist
        try:
            results = _http_json('https://lrclib.net/api/search?' +
                                 urllib.parse.urlencode(params))
        except Exception as e:
            log(f"    LRCLIB lookup failed ({e})")
            return None
        good = [r for r in results if r.get('duration')
                and abs(r['duration'] - dur_s) <= 5]
        good.sort(key=lambda r: (not r.get('syncedLyrics'),
                                 abs(r['duration'] - dur_s)))
        if good:
            r = good[0]
            label = f"{r.get('artistName', '')} – {r.get('trackName', '')}"
            if r.get('instrumental'):
                return {'instrumental': True, 'lines': [], 'label': label}
            if r.get('syncedLyrics'):
                return {'instrumental': False, 'label': label,
                        'lines': _parse_lrc(r['syncedLyrics'])}
    return None


# ═══════════════════════════════════════════════════════════════════════
#  Per-song analysis (cached)
# ═══════════════════════════════════════════════════════════════════════

def _cache_key(seq):
    parts = [str(CACHE_VERSION), seq['path'], seq['media_file']]
    for p in (seq['path'], seq['media_file']):
        try:
            parts.append(str(os.path.getmtime(p)))
        except OSError:
            parts.append('?')
    return hashlib.sha1('|'.join(parts).encode()).hexdigest()


def analyze_song(seq, cache_dir, online=True, log=print):
    """Full analysis for one sequence dict (from SequenceInfo.to_dict())."""
    os.makedirs(cache_dir, exist_ok=True)
    key = _cache_key(seq)
    cpath = os.path.join(cache_dir, key + '.json')
    if os.path.exists(cpath):
        with open(cpath, 'r', encoding='utf-8') as f:
            A = json.load(f)
    else:
        A = _analyze_uncached(seq, log)
        with open(cpath, 'w', encoding='utf-8') as f:
            json.dump(A, f)

    # Online lyrics only when the sequence has none (cached separately)
    if not A['lines'] and online and not A.get('online_checked'):
        log("    No lyric track in sequence — searching LRCLIB…")
        res = fetch_online_lyrics(seq['name'], seq['media_file'],
                                  A.get('meta', {}), A['duration_ms'], log)
        A['online_checked'] = True
        if res:
            if res['instrumental']:
                A['lyrics_source'] = f"LRCLIB says instrumental ({res['label']})"
            elif res['lines']:
                A['lines'] = res['lines']
                A['vocals'] = _merge_intervals([(l['start'], l['end'])
                                                for l in res['lines']])
                A['lyrics_source'] = f"LRCLIB synced lyrics ({res['label']})"
        else:
            A['lyrics_source'] = 'none found online — using audio structure'
        with open(cpath, 'w', encoding='utf-8') as f:
            json.dump(A, f)
    return A


def _analyze_uncached(seq, log):
    t0 = time.time()
    meta, tracks = _read_timing_tracks(seq['path'])
    y = decode_audio(seq['media_file'])
    audio_ms = len(y) / SR * 1000
    duration_ms = int(min(seq['duration_ms'], audio_ms) if audio_ms > 0
                      else seq['duration_ms'])

    energy = _energy_db(y)
    onset, low_onset, fps = _onset_envelopes(y)
    chroma, ctimes = _chroma_frames(y)
    key = _estimate_key(chroma)

    beats, downbeats, beat_name = _sequence_beats(tracks, duration_ms)
    if beats:
        beat_source = f'sequence timing track "{beat_name}"'
    else:
        _, beats = _track_beats(onset, fps)
        beat_source = 'audio beat tracking'
    # Beat tracks marked in half-beats (>180 BPM) -> use every other mark
    while len(beats) > 8 and 60000.0 / float(np.median(np.diff(beats))) > 180:
        idx = np.clip((np.array(beats) / 1000 * fps).astype(int), 0, len(low_onset) - 1)
        phase = int(low_onset[idx[1::2]].mean() > low_onset[idx[0::2]].mean())
        beats = beats[phase::2]
    if not downbeats:
        downbeats = _choose_downbeats(beats, low_onset, fps)
    bpm = 60000.0 / float(np.median(np.diff(beats))) if len(beats) > 4 else 120.0

    title = _clean_title(seq['name'].split(' - ')[0])
    lines, vocals, lyric_track, ignored = _sequence_lyrics(tracks, title, seq['path'])
    lyrics_source = (f'sequence lyric track "{lyric_track}"' if lyric_track
                     else 'none in sequence')
    if ignored:
        lyrics_source += (' (ignored ' + ', '.join(f'"{n}"' for n in ignored) +
                          ' — looks copied from another song)')
        log(f"    ignoring lyric track(s) {ignored}: they don't match this song")

    boundaries = _audio_boundaries(chroma, ctimes, energy, downbeats, duration_ms)
    features = _timbre_features(y, onset, fps, energy)

    log(f"    analysed in {time.time() - t0:.1f}s · {bpm:.0f} BPM · "
        f"{key['name'] if key else '?'} · {len(lines)} lyric lines")
    return {
        'name': seq['name'],
        'meta': meta,
        'duration_ms': duration_ms,
        'bpm': round(bpm, 2),
        'beats': [round(b) for b in beats if b < duration_ms],
        'downbeats': [round(b) for b in downbeats if b < duration_ms],
        'beat_source': beat_source,
        'key': key,
        'energy_db': [round(float(v), 1) for v in energy],
        'lines': lines,
        'vocals': vocals,
        'lyrics_source': lyrics_source,
        'ignored_lyric_tracks': ignored,
        'audio_boundaries': [round(b) for b in boundaries],
        'features': features,
    }


# ═══════════════════════════════════════════════════════════════════════
#  Structure + candidate clips
# ═══════════════════════════════════════════════════════════════════════

def _norm(s):
    return re.sub(r'\s+', ' ', re.sub(r"[^a-z0-9' ]", ' ', s.lower())).strip()


def _similar(a, b):
    if a == b:
        return True
    ta, tb = set(a.split()), set(b.split())
    if len(ta) < 2 or len(tb) < 2:
        return False
    return len(ta & tb) / len(ta | tb) >= 0.7


class Song:
    def __init__(self, A, title):
        self.A = A
        self.title = title
        self.dur = A['duration_ms']
        self.bpm = A['bpm']
        self.beat_ms = 60000.0 / max(40, self.bpm)
        self.db = np.array(A['energy_db'] or [-60.0])
        voiced = self.db[self.db > self.db.max() - 40]
        self.e_mean = float(voiced.mean()) if len(voiced) else float(self.db.mean())
        self.e_std = float(voiced.std()) + 1e-6 if len(voiced) else 1.0
        self.downbeats = sorted(A['downbeats']) or \
            list(np.arange(0, self.dur, self.beat_ms * 4))
        self.vocals = A['vocals']
        self.vstarts = [v[0] for v in self.vocals]
        self.lines = [l for l in A['lines'] if l['end'] > l['start']]

    # helpers ------------------------------------------------------------
    def energy(self, a, b):
        i = max(0, int(a / 100))
        j = max(i + 1, int(b / 100))
        seg = self.db[i:j]
        return float(seg.mean()) if len(seg) else self.e_mean - 10

    def db_before(self, t):
        i = bisect.bisect_right(self.downbeats, t) - 1
        return self.downbeats[i] if i >= 0 else None

    def db_after(self, t):
        i = bisect.bisect_left(self.downbeats, t)
        return self.downbeats[i] if i < len(self.downbeats) else None

    def vocal_overlap(self, a, b):
        if b <= a:
            return 0
        tot = 0
        i = max(0, bisect.bisect_right(self.vstarts, a) - 1)
        while i < len(self.vocals) and self.vocals[i][0] < b:
            s, t = self.vocals[i]
            tot += max(0, min(b, t) - max(a, s))
            i += 1
        return tot

    def next_vocal_after(self, t):
        for s, e in self.vocals:
            if s >= t:
                return s
        return None

    # structure ----------------------------------------------------------
    def blocks(self):
        if len(self.lines) >= 4:
            return self._lyric_blocks(), False
        return self._audio_blocks(), True

    def _lyric_blocks(self):
        L = self.lines
        texts = [_norm(l['text']) for l in L]
        rep = []
        for i, a in enumerate(texts):
            rep.append(bool(a) and any(
                j != i and abs(L[i]['start'] - L[j]['start']) > 12000
                and _similar(a, texts[j]) for j in range(len(L))))
        for i in range(1, len(rep) - 1):          # smooth single outliers
            if rep[i - 1] == rep[i + 1] != rep[i]:
                rep[i] = rep[i - 1]

        gap_split = max(1.6 * 4 * self.beat_ms, 2500)
        groups, cur = [], [0]
        for i in range(1, len(L)):
            gap = L[i]['start'] - L[i - 1]['end']
            if gap >= gap_split or rep[i] != rep[i - 1]:
                groups.append(cur)
                cur = []
            cur.append(i)
        groups.append(cur)

        # split overly long blocks at their biggest internal gap
        def split(g):
            if L[g[-1]]['end'] - L[g[0]]['start'] <= 32000 or len(g) < 4:
                return [g]
            lo, hi = int(len(g) * 0.3), int(len(g) * 0.7)
            k = max(range(max(1, lo), max(lo + 1, hi)),
                    key=lambda k: L[g[k]]['start'] - L[g[k - 1]]['end'])
            return split(g[:k]) + split(g[k:])
        groups = [s for g in groups for s in split(g)]

        title_toks = set(_norm(self.title).split()) - {'the', 'a', 'of', 'is', 'you', 'i'}
        blocks, n_ch, n_vs, prev, prev_end, sec_first = [], 0, 0, None, -1e9, None
        for g in groups:
            chorus = sum(rep[i] for i in g) >= len(g) / 2
            text = ' '.join(texts[i] for i in g)
            hook = bool(title_toks) and \
                len(title_toks & set(text.split())) >= max(1, 0.6 * len(title_toks))
            # adjacent blocks of the same kind belong to the same section,
            # unless an instrumental break separates them
            new_section = (prev is not chorus or
                           L[g[0]]['start'] - prev_end >= gap_split or
                           # the chorus's opening line coming round again
                           (chorus and _similar(texts[g[0]], sec_first)))
            if new_section:
                sec_first = texts[g[0]]
            if chorus:
                n_ch += new_section
                label = f"Chorus {n_ch}"
            else:
                n_vs += new_section
                label = f"Verse {n_vs}"
            prev, prev_end = chorus, L[g[-1]]['end']
            blocks.append({'start': L[g[0]]['start'], 'end': L[g[-1]]['end'],
                           'chorus': chorus, 'hook': hook, 'label': label,
                           'first': L[g[0]]['text'], 'last': L[g[-1]]['text']})
        return blocks

    def _audio_blocks(self):
        db = self.downbeats
        bounds = sorted(set([db[0] if db else 0.0] + list(self.A['audio_boundaries'])
                            + [float(self.dur)]))
        raw = [(a, b) for a, b in zip(bounds[:-1], bounds[1:]) if b - a >= 4000]
        if not raw:
            raw = [(0.0, float(self.dur))]
        en = [self.energy(a, b) for a, b in raw]
        thr = float(np.percentile(en, 60))
        blocks = []
        for k, ((a, b), e) in enumerate(zip(raw, en), 1):
            high = e >= thr
            blocks.append({'start': a, 'end': b, 'chorus': high, 'hook': False,
                           'label': f"Part {k}" + (" (peak)" if high else ""),
                           'first': '', 'last': ''})
        return blocks

    # candidates ---------------------------------------------------------
    def candidates(self, target_ms, k=8, rng=None):
        blocks, instr = self.blocks()
        self.instrumental = instr
        bar = 4 * self.beat_ms
        starts, ends = [], []
        for i, b in enumerate(blocks):
            if instr:
                starts.append((i, b['start'], 0, False))
            else:
                s = self.db_before(b['start'] - 40)
                if s is None or b['start'] - s > 2.2 * bar:
                    s = max(0, b['start'] - min(bar, 1500))
                clash = self.vocal_overlap(s, b['start'] - 40)
                starts.append((i, s, clash, False))
        if blocks and 1500 < blocks[0]['start'] <= 25000 and not instr:
            starts.append((0, 0, 0, True))                      # opening

        for j, b in enumerate(blocks):
            if instr:
                ends.append((j, b['end'], 0, False, False))
                continue
            le = b['end']
            e = self.db_after(le + 120)
            if e is None or e - le > 2.2 * bar:
                e = min(self.dur, le + min(bar, 1500))
            ov = self.vocal_overlap(le + 120, e)
            offgrid = False
            if ov > 350:
                nv = self.next_vocal_after(le + 60) or e
                e = max(le + 200, min(e, nv - 80))
                ov = self.vocal_overlap(le + 120, e)
                offgrid = True
            ends.append((j, min(e, self.dur), ov, offgrid, False))
        if blocks and self.dur - blocks[-1]['end'] < 35000:
            ends.append((len(blocks) - 1, self.dur, 0, False, True))  # closing

        segs = []
        for i, s, clash, opening in starts:
            for j, e, ov, offgrid, closing in ends:
                if j < i or e <= s:
                    continue
                dur = e - s
                if not (0.6 * target_ms <= dur <= 1.7 * target_ms):
                    continue
                inc = blocks[i:j + 1]
                w = 0.6 if instr else 1.0
                sc = 0.0
                n_ch = sum(b['chorus'] for b in inc)
                if n_ch:
                    sc += w * (3 + 0.5 * min(n_ch - 1, 1))
                if inc[-1]['chorus']:
                    sc += w * 1.2
                if not inc[0]['chorus'] and inc[-1]['chorus']:
                    sc += w * 0.6
                elif inc[0]['chorus']:
                    sc += w * 0.3
                if any(b['hook'] and b['chorus'] for b in inc):
                    sc += 0.8
                # too short sounds choppy, too long drags: both cost, short more
                r = dur / target_ms
                sc -= 5 * math.log(1 / r) if r < 1 else 4 * math.log(r)
                z = (self.energy(s, e) - self.e_mean) / self.e_std
                sc += (1.5 if instr else 0.8) * max(-2, min(2, z))
                sc -= 0.5 * s / self.dur
                sc -= min(3, clash / 1000 * 3)
                sc -= min(4, ov / 1000 * 2.5)
                if offgrid:
                    sc -= 0.6
                if rng is not None:         # variety between generations
                    sc += rng.gauss(0, 0.7)
                segs.append({
                    's': float(s), 'e': float(e), 'score': sc,
                    'opening': opening, 'closing': closing,
                    'label': ' → '.join(
                        lb for k2, lb in enumerate(b['label'] for b in inc)
                        if k2 == 0 or lb != inc[k2 - 1]['label']),
                    'first_line': inc[0]['first'], 'last_line': inc[-1]['last'],
                    'head_ms': (inc[0]['start'] - s),
                    'tail_ms': (e - inc[-1]['end']),
                    'clean': clash < 200 and ov < 200,
                })

        if not segs:          # fall back: best-energy window of target length
            step = max(1000, self.dur // 60)
            best = max(range(0, max(1, int(self.dur - target_ms)), int(step)),
                       key=lambda t: self.energy(t, t + target_ms), default=0)
            s = self.db_before(best) or 0
            e = min(self.dur, (self.db_after(s + target_ms) or s + target_ms))
            segs.append({'s': float(s), 'e': float(e), 'score': 0.0,
                         'opening': False, 'closing': e >= self.dur,
                         'label': 'Loudest section', 'first_line': '',
                         'last_line': '', 'head_ms': 0, 'tail_ms': 0,
                         'clean': True})

        segs.sort(key=lambda x: -x['score'])
        chosen = []
        for sg in segs:
            if all(abs(sg['s'] - c['s']) > 4000 or abs(sg['e'] - c['e']) > 4000
                   for c in chosen):
                chosen.append(sg)
            if len(chosen) >= k:
                break
        for flag in ('opening', 'closing'):
            if not any(c[flag] for c in chosen):
                extra = next((sg for sg in segs if sg[flag]), None)
                if extra:
                    chosen.append(extra)
        for c in chosen:
            c['lead_db'] = self.energy(c['s'], c['s'] + 3000)
            c['tail_db'] = self.energy(c['e'] - 3000, c['e'])
        return chosen


# ═══════════════════════════════════════════════════════════════════════
#  Transitions + ordering
# ═══════════════════════════════════════════════════════════════════════

def _camelot_dist(a, b):
    if not a or not b:
        return 2
    na, la = int(a[:-1]), a[-1]
    nb, lb = int(b[:-1]), b[-1]
    d = min((na - nb) % 12, (nb - na) % 12)
    return d + (0 if la == lb else 1) if d else (0 if la == lb else 1)


def _tempo_diff(a, b):
    return min(abs(math.log(a * f / b)) for f in (0.5, 1, 2))


def transition_cost(sa, ca, sb, cb):
    tempo = 8 * _tempo_diff(sa.bpm, sb.bpm)
    ka = (sa.A.get('key') or {}).get('camelot')
    kb = (sb.A.get('key') or {}).get('camelot')
    d = _camelot_dist(ka, kb)
    key = [0, 0.2, 0.6][d] if d < 3 else 0.6 + 0.3 * (d - 2)
    level = 0.06 * max(0, abs(ca['tail_db'] - cb['lead_db']) - 3)
    clash = 0.4 if (ca['tail_ms'] < 250 and cb['head_ms'] < 300) else 0
    return tempo + key + level + clash


OPEN_BONUS, CLOSE_BONUS = 1.2, 1.2


def _node_value(c, pos):
    v = c['score']
    if pos == 'only':
        return v
    if c['opening']:
        v += OPEN_BONUS if pos == 'first' else -0.4
    if c['closing']:
        v += CLOSE_BONUS if pos == 'last' else -1.5
    return v


MAX_USES = 3            # a song may appear at most this many times
REUSE_PENALTY = 2.0     # each visit beyond the required ones must earn its place
THIRD_VISIT_PENALTY = 1.0
PINGPONG_PENALTY = 1.2  # A → B → A straight away sounds indecisive
BOOKEND_BONUS = 2.0     # opening and closing on the same song feels deliberate
OVERLAP_TOL = 500       # visits to the same song may not overlap by more than this
BEAM_WIDTH = 250


def _overlaps(seg, used):
    return any(seg['s'] < b - OVERLAP_TOL and a < seg['e'] - OVERLAP_TOL
               for a, b in used)


def _search(cands, Tc, min_uses, fixed_seq, max_ms, soft_ms, target_ms,
            allow_reuse):
    """
    Beam search for the running order: a list of (song, candidate) slots.

    * every song appears at least min_uses[j] times (if it fits)
    * with allow_reuse a song may come back (up to MAX_USES), never back to
      back and never replaying a part of the song already used
    * total length never exceeds max_ms; going past soft_ms costs score
    * fixed_seq (list of song ids) pins the order, only clip choice varies

    Returns (slots, all_songs_fit).
    """
    n = len(cands)
    best_mid = [max(_node_value(c, 'mid') for c in cands[j]) for j in range(n)]
    min_dur = [min(c['e'] - c['s'] for c in cands[j]) for j in range(n)]
    # Reserve room for songs not yet placed, so nothing gets squeezed out
    # (only when every song can fit at all)
    reserve = sum(min_dur[j] * min_uses[j] for j in range(n)) <= max_ms

    def owed_time(uses):
        return sum(min_dur[j] * max(0, min_uses[j] - uses[j]) for j in range(n))

    def heuristic(uses):            # optimistic value of songs still owed
        return sum(best_mid[j] * max(0, min_uses[j] - uses[j]) for j in range(n))

    def finalize(v, slots, total):
        j, c = slots[-1]
        cd = cands[j][c]
        if len(slots) == 1:
            v += _node_value(cd, 'only') - _node_value(cd, 'first')
        else:
            v += _node_value(cd, 'last') - _node_value(cd, 'mid')
            if len(slots) >= 3 and slots[0][0] == j:
                v += BOOKEND_BONUS
        if total > soft_ms:
            v -= 3.0 * (total - soft_ms) / target_ms
        return v

    if fixed_seq:
        max_steps = len(fixed_seq)
    else:
        max_steps = min(24, sum(min_uses) + (n if allow_reuse else 0))

    beam = [(0.0, (), (0,) * n, ((),) * n, 0.0)]
    best_done = None                 # (score, slots)
    best_partial = None              # (coverage, score, slots)
    for step in range(max_steps):
        nxt = []
        for g, slots, uses, used, total in beam:
            last = slots[-1] if slots else None
            for j in ([fixed_seq[step]] if fixed_seq else range(n)):
                if not fixed_seq:
                    if last and last[0] == j:
                        continue
                    cap = max(min_uses[j], MAX_USES if allow_reuse else 0)
                    if uses[j] >= cap:
                        continue
                nu = list(uses)
                nu[j] += 1
                owed_after = owed_time(nu) if reserve else 0
                for c, cd in enumerate(cands[j]):
                    dur = cd['e'] - cd['s']
                    if total + dur + owed_after > max_ms or _overlaps(cd, used[j]):
                        continue
                    v = g + _node_value(cd, 'mid' if slots else 'first')
                    if last:
                        v -= Tc[last[0]][last[1]][j][c]
                    if uses[j] >= min_uses[j]:
                        v -= REUSE_PENALTY + (THIRD_VISIT_PENALTY if uses[j] >= 2 else 0)
                    if not fixed_seq and len(slots) >= 2 and slots[-2][0] == j:
                        v -= PINGPONG_PENALTY
                    nused = list(used)
                    nused[j] = used[j] + ((cd['s'], cd['e']),)
                    nxt.append((v, slots + ((j, c),), tuple(nu),
                                tuple(nused), total + dur))
        if not nxt:
            break
        for v, slots, uses, used, total in nxt:
            fv = finalize(v, slots, total)
            cov = sum(1 for j in range(n) if uses[j] >= min_uses[j])
            done = cov == n and (not fixed_seq or len(slots) == len(fixed_seq))
            if done and (best_done is None or fv > best_done[0]):
                best_done = (fv, slots)
            if best_partial is None or (cov, fv) > best_partial[:2]:
                best_partial = (cov, fv, slots)
        nxt.sort(key=lambda st: -(st[0] + heuristic(st[2])))
        beam = nxt[:BEAM_WIDTH]

    if best_done:
        return list(best_done[1]), True
    return (list(best_partial[2]) if best_partial else []), False


# ═══════════════════════════════════════════════════════════════════════
#  Public entry point
# ═══════════════════════════════════════════════════════════════════════

def _fmt(ms):
    ms = max(0, int(ms))
    return f"{ms // 60000}:{ms // 1000 % 60:02d}.{ms // 100 % 10}"


def _short(s, n=48):
    s = s.strip()
    return s if len(s) <= n else s[:n - 1].rstrip() + '…'


def _ordinal(k):
    return {1: '1st', 2: '2nd', 3: '3rd'}.get(k, f'{k}th')


def _scramble(slot_song, rng):
    """
    Random running order for the given slots. Prefers orders where the same
    song isn't back-to-back and that differ from the current order.
    """
    best, best_key = list(slot_song), None
    for _ in range(300):
        order = list(slot_song)
        rng.shuffle(order)
        adjacent = sum(1 for a, b in zip(order, order[1:]) if a == b)
        key = (adjacent, order == list(slot_song))
        if best_key is None or key < best_key:
            best, best_key = order, key
        if key == (0, False):
            break
    return best


def plan_mashup(seqs, cache_dir, target_s=30, allow_reorder=True,
                allow_reuse=True, max_total_s=None, online=True,
                crossfade_ms=None, progress=None, log=print, seed=None,
                order_mode=None):
    """
    seqs: list of sequence dicts, one per clip in the user's current order
          (the same song may appear several times).
    order_mode: 'best'     — choose the order for the smoothest flow
                             (tempo, key, energy); songs may come back
                'scramble' — random order (new every run), best cuts kept
                'keep'     — keep the user's clip order, just pick the cuts
                None       — 'best' if allow_reorder else 'keep' (legacy)
    allow_reuse: ('best' only) a song may come back for another section.
    max_total_s: hard cap on the mashup length (None = no cap).
    crossfade_ms: None = choose automatically, else use the given value.
    """
    progress = progress or (lambda f, m: None)

    # Unique songs; a song added twice by the user must appear at least twice
    song_paths, slot_song = [], []
    for seq in seqs:
        if seq['path'] not in song_paths:
            song_paths.append(seq['path'])
        slot_song.append(song_paths.index(seq['path']))
    first_slot = [slot_song.index(j) for j in range(len(song_paths))]
    useqs = [seqs[i] for i in first_slot]
    n = len(useqs)
    min_uses = [slot_song.count(j) for j in range(n)]

    analyses = []
    for idx, seq in enumerate(useqs):
        progress(idx / (n + 1), f"Listening to “{seq['name']}” ({idx + 1}/{n})…")
        log(f"  [{idx + 1}/{n}] {seq['name']}")
        analyses.append(analyze_song(seq, cache_dir, online=online, log=log))

    progress(n / (n + 1), "Finding the best cut points and running order…")
    songs = [Song(A, _clean_title(seq['name'].split(' - ')[0]))
             for A, seq in zip(analyses, useqs)]

    # Fit the per-song length to the maximum mashup length if needed
    max_ms = float(max_total_s) * 1000 if max_total_s else float('inf')
    target_ms = max(8, float(target_s)) * 1000
    n_slots = len(seqs)
    shrunk = False
    if n_slots * target_ms > max_ms * 0.9:
        # leave headroom: clips snap to section boundaries, so they vary
        target_ms = max(8000.0, max_ms / n_slots * 0.85)
        shrunk = True
    if order_mode not in ('best', 'scramble', 'keep'):
        order_mode = 'best' if allow_reorder else 'keep'
    if order_mode == 'scramble':
        srng = random.Random(seed if seed is not None else time.time_ns())
        fixed_seq = _scramble(slot_song, srng)
    elif order_mode == 'keep':
        fixed_seq = slot_song
    else:
        fixed_seq = None
    reuse = allow_reuse and order_mode == 'best'
    # with reuse, leave room for roughly one return visit per two songs
    soft_ms = min(max_ms, n_slots * target_ms * (1.5 if reuse else 1.0))

    rng = random.Random(seed) if seed is not None else None
    cands = [s.candidates(target_ms, rng=rng) for s in songs]
    Tc = [[[[transition_cost(songs[i], ca, songs[j], cb) if i != j else 0.5
             for cb in cands[j]] for j in range(n)]
           for ca in cands[i]] for i in range(n)]

    slots, all_fit = _search(cands, Tc, min_uses, fixed_seq,
                             max_ms, soft_ms, target_ms, reuse)
    if not slots:
        raise ValueError('No clips fit within the maximum mashup length — '
                         'try a longer maximum or fewer songs.')
    used_songs = {j for j, _ in slots}
    dropped = [useqs[j]['name'] for j in range(n) if j not in used_songs]

    if crossfade_ms is None:
        beat = float(np.median([s.beat_ms for s in songs]))
        xf = int(min(1500, max(400, round(2 * beat / 50) * 50)))
        xf_auto = True
    else:
        xf = int(crossfade_ms)
        xf_auto = False

    m = len(slots)
    visits = [0] * n
    clips = []
    for k, (i, c) in enumerate(slots):
        song, seg = songs[i], cands[i][c]
        visits[i] += 1
        s, e = seg['s'], seg['e']
        if xf and m > 1:                      # centre the fade on the bar line
            if k > 0:
                s = max(0, s - xf / 2)
            if k < m - 1:
                e = min(song.dur, e + xf / 2)
        why = [seg['label']]
        if visits[i] > 1:
            why.insert(0, f"{_ordinal(visits[i])} visit")
        if seg['opening']:
            why.append('starts from the song\'s intro')
        if seg['first_line']:
            why.append(f"from “{_short(seg['first_line'])}”")
        if seg['closing']:
            why.append('ends on the song\'s real ending')
        elif seg['last_line']:
            why.append(f"ends after “{_short(seg['last_line'])}”")
        key = (song.A.get('key') or {})
        clips.append({
            'input_index': first_slot[i],
            'xsq_path': useqs[i]['path'],
            'name': useqs[i]['name'],
            'visit': visits[i],
            'start_ms': int(round(s)),
            'end_ms': int(round(e)),
            'note': ' · '.join(why),
            'bpm': round(song.bpm),
            'key': key.get('name'),
            'camelot': key.get('camelot'),
            'clean_cuts': seg['clean'],
            'lyrics_source': song.A.get('lyrics_source'),
            'beat_source': song.A.get('beat_source'),
        })

    transitions = []
    for k in range(m - 1):
        a, b = songs[slots[k][0]], songs[slots[k + 1][0]]
        ka = (a.A.get('key') or {}).get('camelot')
        kb = (b.A.get('key') or {}).get('camelot')
        d = _camelot_dist(ka, kb)
        tdiff = (math.exp(_tempo_diff(a.bpm, b.bpm)) - 1) * 100
        verdict = ('smooth' if tdiff < 4 and d <= 1 else
                   'good' if tdiff < 8 and d <= 2 else 'bold')
        transitions.append({
            'text': f"{round(a.bpm)}→{round(b.bpm)} BPM ({tdiff:.0f}% apart) · "
                    f"key {ka or '?'}→{kb or '?'}"
                    f"{' (compatible)' if d <= 1 else ''}",
            'verdict': verdict,
        })

    progress(1.0, 'Done')
    return {
        'clips': clips,
        'transitions': transitions,
        'crossfade_ms': xf,
        'crossfade_auto': xf_auto,
        'reordered': [j for j, _ in slots] != slot_song,
        'order_mode': order_mode,
        'total_ms': int(sum(cands[i][c]['e'] - cands[i][c]['s'] for i, c in slots)),
        'max_ms': None if max_ms == float('inf') else int(max_ms),
        'target_used_s': round(target_ms / 1000),
        'target_shrunk': shrunk,
        'dropped': dropped,
        'all_fit': all_fit,
        'revisits': sum(1 for c in clips if c['visit'] > 1),
    }
