"""
Song Library: classification + mashup song picker
=================================================

Classification (per song, cached in library_cache/library.json):

  Themes   from the lyrics (sequence lyric track or LRCLIB), the song title,
           folder and file name, using weighted word lists:
           Christmas, Halloween, Faith, Comedy, Intros & Talking
  Genres   from MusicBrainz (online, optional): recording tags + the
           artist's curated genres, mapped to Pop, Rock, Country, Dance,
           Hip-Hop & R&B, Jazz & Classics, Orchestral, A Cappella,
           Movie & Musical, Kids (and Christmas/Comedy/Faith when tagged)
  Moods    from the audio, relative to the rest of the library:
           Upbeat, Ballad / Slow, Bass-heavy, Instrumental

The user can override any song's categories (library_overrides.json).

Picking (generate):
  * pool = songs matching the chosen categories (all / any), minus songs
    the user removed; songs the user added are always kept
  * songs are added one by one: score = mashup-ability (has a repeated
    chorus, lyrics, energy) + musical compatibility with the songs already
    picked (tempo, key, bass weight, loudness) - recent use + Gumbel noise
  * the exact same song set is never returned twice in a row history
    (generator_history.json); if the pool forces the same set, the plan
    itself (order / cut points) is re-rolled until it's new
"""

import os
import re
import json
import math
import time
import random
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter

import numpy as np

import automix

LIBRARY_VERSION = 2
MB_USER_AGENT = 'xLightsMashupBuilder/1.0 (personal holiday-lights tool)'

THEMES = ['Christmas', 'Halloween', 'Faith', 'Comedy', 'Movie & Musical',
          'Kids', 'Intros & Talking']
GENRES = ['Pop', 'Rock', 'Country', 'Dance', 'Hip-Hop & R&B',
          'Jazz & Classics', 'Orchestral', 'A Cappella']
MOODS = ['Upbeat', 'Ballad / Slow', 'Bass-heavy', 'Instrumental']
ALL_CATEGORIES = THEMES + GENRES + MOODS

# word -> weight (3 = unmistakable, 1 = supporting)
LEXICON = {
    'Christmas': {
        **dict.fromkeys(['christmas', 'xmas', 'santa', 'claus', 'sleigh', 'sleighbells',
                         'reindeer', 'rudolph', 'snowman', 'frosty', 'jingle', 'mistletoe',
                         'noel', 'navidad', 'yule', 'yuletide', 'tinsel', 'stocking',
                         'stockings', 'chimney', 'elf', 'elves', 'nutcracker', 'carol',
                         'carols', 'caroling', 'gingerbread', 'manger', 'bethlehem',
                         'nativity', 'feliz', 'holly'], 3),
        **dict.fromkeys(['snow', 'snowing', 'snowflake', 'snowflakes', 'bells', 'merry',
                         'winter', 'december', 'presents', 'gifts', 'ornaments', 'wreath',
                         'mittens', 'fireplace', 'cocoa', 'shepherds', 'tree'], 1),
    },
    'Halloween': {
        **dict.fromkeys(['halloween', 'spooky', 'zombie', 'zombies',
                         'vampire', 'vampires', 'werewolf', 'haunted', 'ghoul', 'ghouls',
                         'goblin', 'goblins', 'frankenstein', 'dracula', 'cauldron',
                         'graveyard', 'trick-or-treat', 'thriller'], 3),
        **dict.fromkeys(['ghost', 'ghosts', 'witch', 'witches', 'monster', 'monsters',
                         'skeleton', 'skeletons', 'boo', 'creepy', 'scream', 'fright',
                         'bats', 'mummy', 'coffin', 'grave', 'haunt', 'scary',
                         'pumpkin', 'pumpkins'], 1),       # pumpkin pie is Christmas too
    },
    'Faith': {
        **dict.fromkeys(['jesus', 'christ', 'savior', 'saviour', 'hallelujah', 'alleluia',
                         'emmanuel', 'immanuel', 'messiah', 'lord', 'amen', 'gloria',
                         'excelsis'], 3),
        **dict.fromkeys(['god', 'holy', 'praise', 'worship', 'glory', 'heaven', 'heavenly',
                         'angels', 'angel', 'grace', 'pray', 'almighty', 'king', 'born',
                         'faith'], 1),
    },
}
NAME_HINTS = [
    ('Comedy', r'\b(comedy|comical|funny|parody|novelty)\b'),
    ('Intros & Talking', r'\b(talking|intro|outro|welcome|announcement|safety|tune to)\b'),
    ('Halloween', r'\bhalloween\b'),
]
TAG_RULES = [
    ('Christmas', ['christmas', 'holiday']),
    ('Halloween', ['halloween']),
    ('Faith', ['christian', 'gospel', 'worship', 'ccm', 'praise']),
    ('Comedy', ['comedy', 'novelty', 'parody', 'humour', 'humor']),
    ('Movie & Musical', ['soundtrack', 'musical', 'show tunes', 'film score', 'disney',
                         'broadway']),
    ('Kids', ["children's", 'children', 'kids']),
    ('Country', ['country', 'bluegrass']),
    ('Rock', ['rock', 'metal', 'punk', 'grunge']),
    ('Pop', ['pop']),
    ('Dance', ['dance', 'edm', 'electronic', 'house', 'disco', 'electro', 'techno',
               'synthwave']),
    ('Hip-Hop & R&B', ['hip hop', 'hip-hop', 'rap', 'r&b', 'rnb', 'soul', 'funk', 'trap']),
    ('Jazz & Classics', ['jazz', 'swing', 'big band', 'easy listening', 'traditional pop',
                         'crooner', 'exotica', 'rockabilly']),
    ('Orchestral', ['classical', 'orchestral', 'symphonic', 'instrumental', 'new age',
                    'neoclassical']),
    ('A Cappella', ['a cappella', 'acappella', 'a capella']),
]


# ═══════════════════════════════════════════════════════════════════════
#  Storage
# ═══════════════════════════════════════════════════════════════════════

class Store:
    def __init__(self, base_dir):
        self.dir = os.path.join(base_dir, 'library_cache')
        os.makedirs(self.dir, exist_ok=True)
        self.lib_path = os.path.join(self.dir, 'library.json')
        self.mb_path = os.path.join(self.dir, 'musicbrainz_cache.json')
        self.hist_path = os.path.join(self.dir, 'generator_history.json')
        self.over_path = os.path.join(base_dir, 'library_overrides.json')

    def _load(self, path, default):
        try:
            with open(path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except (OSError, ValueError):
            return default

    def _save(self, path, data):
        tmp = path + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=1)
        os.replace(tmp, path)

    def library(self):
        d = self._load(self.lib_path, {})
        return d if d.get('version') == LIBRARY_VERSION else {'version': LIBRARY_VERSION,
                                                              'songs': {}}

    def save_library(self, d):
        self._save(self.lib_path, d)

    def mb(self):
        return self._load(self.mb_path, {})

    def save_mb(self, d):
        self._save(self.mb_path, d)

    def overrides(self):
        return self._load(self.over_path, {})

    def save_overrides(self, d):
        self._save(self.over_path, d)

    def history(self):
        return self._load(self.hist_path, [])

    def save_history(self, h):
        self._save(self.hist_path, h[-200:])


# ═══════════════════════════════════════════════════════════════════════
#  MusicBrainz genres
# ═══════════════════════════════════════════════════════════════════════

_last_mb = [0.0]


def _mb_get(url):
    wait = 1.1 - (time.time() - _last_mb[0])         # MusicBrainz: max 1 req/s
    if wait > 0:
        time.sleep(wait)
    for attempt in range(2):
        _last_mb[0] = time.time()
        req = urllib.request.Request(url, headers={'User-Agent': MB_USER_AGENT,
                                                   'Accept': 'application/json'})
        try:
            with urllib.request.urlopen(req, timeout=15) as r:
                return json.loads(r.read().decode('utf-8'))
        except urllib.error.HTTPError as e:
            if e.code == 503 and attempt == 0:
                time.sleep(2.5)
                continue
            raise


def _norm(s):
    return re.sub(r'[^a-z0-9]+', ' ', s.lower()).strip()


def musicbrainz_tags(seq, meta, log=print):
    """Genre/tag names for a song, or None if it couldn't be identified."""
    pairs = [(t, a) for t, a in automix._title_candidates(seq['name'], seq['media_file'], meta)
             if a][:3]
    for title, artist in pairs:
        q = f'recording:"{title}" AND artist:"{artist}"'
        try:
            res = _mb_get('https://musicbrainz.org/ws/2/recording/?' +
                          urllib.parse.urlencode({'query': q, 'fmt': 'json', 'limit': 5}))
        except Exception as e:
            log(f"    MusicBrainz lookup failed ({e})")
            return None
        for rec in res.get('recordings', []):
            if rec.get('score', 0) < 85:
                continue
            if _norm(title) not in _norm(rec.get('title', '')) and \
                    _norm(rec.get('title', '')) not in _norm(title):
                continue
            tags = Counter()
            for t in rec.get('tags', []):
                tags[t['name']] += t.get('count', 1) + 1
            credit = (rec.get('artist-credit') or [{}])[0]
            aid = (credit.get('artist') or {}).get('id')
            if aid:
                try:
                    art = _mb_get(f'https://musicbrainz.org/ws/2/artist/{aid}?' +
                                  urllib.parse.urlencode({'inc': 'genres+tags', 'fmt': 'json'}))
                    for g in art.get('genres', []):
                        tags[g['name']] += g.get('count', 1) + 2
                    for t in art.get('tags', []):
                        tags[t['name']] += t.get('count', 1)
                except Exception as e:
                    log(f"    MusicBrainz artist lookup failed ({e})")
            label = f"{credit.get('name', artist)} – {rec.get('title', title)}"
            return {'tags': [t for t, _ in tags.most_common(10)], 'label': label}
    return {'tags': [], 'label': None}


# ═══════════════════════════════════════════════════════════════════════
#  Classification
# ═══════════════════════════════════════════════════════════════════════

def _words(text):
    return re.findall(r"[a-z][a-z'\-]*", text.lower())


def _theme_scores(lyrics_text, name_text):
    lw = _words(lyrics_text)
    nw = set(_words(name_text))
    out = {}
    for cat, lex in LEXICON.items():
        cnt = Counter(w for w in lw if w in lex)
        score = sum(lex[w] * min(c, 6) for w, c in cnt.items())
        strong = sum(c for w, c in cnt.items() if lex[w] >= 3)
        strong_distinct = sum(1 for w in cnt if lex[w] >= 3)
        title_hit = [w for w in nw if lex.get(w, 0) >= 3]
        density = score / max(60, len(lw)) * 100
        out[cat] = {'density': density, 'strong': strong, 'title': title_hit,
                    'strong_distinct': strong_distinct,
                    'top': [w for w, _ in cnt.most_common(4)]}
    return out


def classify(seq, A, mb, lib_stats):
    """Return (categories, reasons) for one song."""
    cats, why = set(), {}
    name_text = ' '.join([seq['name'], seq.get('folder', ''),
                          os.path.splitext(os.path.basename(seq['media_file']))[0]])
    lyrics = ' '.join(l['text'] for l in A.get('lines', []))
    th = _theme_scores(lyrics, name_text)

    c = th['Christmas']
    if c['title'] or (c['strong'] >= 1 and c['density'] >= 3.0):
        cats.add('Christmas')
        why['Christmas'] = ('title: ' + ', '.join(c['title'])) if c['title'] else \
            'lyrics: ' + ', '.join(c['top'])
    h = th['Halloween']
    if h['title'] or (h['strong'] >= 1 and h['density'] >= 2.5):
        cats.add('Halloween')
        why['Halloween'] = 'lyrics: ' + ', '.join(h['top'] or h['title'])
    f = th['Faith']
    if f['strong_distinct'] >= 2 and f['density'] >= 3.0:
        cats.add('Faith')
        why['Faith'] = 'lyrics: ' + ', '.join(f['top'])
    for cat, rx in NAME_HINTS:
        if re.search(rx, name_text, re.I):
            cats.add(cat)
            why.setdefault(cat, 'name')

    if mb and mb.get('tags'):
        top = mb['tags'][:6]            # strongest tags only; long tails over-tag
        for cat, keys in TAG_RULES:
            hits = [t for t in top if any(k in t.lower() for k in keys)]
            if hits:
                cats.add(cat)
                why.setdefault(cat, f"MusicBrainz: {', '.join(hits[:3])}")

    # Moods from audio, relative to the library
    feat = A.get('features') or {}
    bpm = A.get('bpm') or 120
    has_lyrics = len(A.get('lines', [])) >= 4
    if not has_lyrics and 'Intros & Talking' not in cats:
        cats.add('Instrumental')
        why['Instrumental'] = A.get('lyrics_source') or 'no lyrics found'
    # Beat density + loudness first: detected tempo can be off by 2x
    dens = feat.get('onset_density', 0)
    loud = feat.get('loudness_db', -30)
    if (dens >= lib_stats['dens_p50'] and loud >= lib_stats['loud_p40']) or \
            (bpm >= 115 and dens >= lib_stats['dens_p35']):
        cats.add('Upbeat')
        why['Upbeat'] = f"busy beat ({dens:.1f} hits/s), {bpm:.0f} BPM"
    elif dens <= lib_stats['dens_p35'] and (bpm <= 100 or loud <= lib_stats['loud_p50']):
        cats.add('Ballad / Slow')
        why['Ballad / Slow'] = f"sparse beat ({dens:.1f} hits/s), {bpm:.0f} BPM"
    if feat.get('bass_ratio', 0) >= lib_stats['bass_p75']:
        cats.add('Bass-heavy')
        why['Bass-heavy'] = f"{feat['bass_ratio'] * 100:.0f}% of energy below 150 Hz"

    ordered = [c for c in ALL_CATEGORIES if c in cats]
    return ordered, why


def _lib_stats(analyses):
    feats = [A.get('features') or {} for A in analyses]
    def pct(key, p, default):
        v = [f[key] for f in feats if key in f]
        return float(np.percentile(v, p)) if v else default
    return {'loud_p40': pct('loudness_db', 40, -20), 'loud_p50': pct('loudness_db', 50, -18),
            'dens_p50': pct('onset_density', 50, 2.5), 'dens_p35': pct('onset_density', 35, 2),
            'bass_p75': pct('bass_ratio', 75, 0.3)}


def _mashability(A, title):
    s = automix.Song(A, title)
    blocks, instr = s.blocks()
    n_chorus = len({b['label'] for b in blocks if b['chorus']})
    q = 0.0
    if not instr:
        q += 0.6
        q += 1.0 if n_chorus >= 2 else (0.4 if n_chorus == 1 else 0)
    if A['duration_ms'] < 70000:
        q -= 1.0
    return round(q, 2), n_chorus


def scan(seqs, store, analysis_cache_dir, online=True, progress=None, log=print):
    """Analyse + classify every sequence. Incremental and cached."""
    progress = progress or (lambda f, m: None)
    lib = store.library()
    mbc = store.mb()
    analyses, todo = {}, []
    n = len(seqs)
    for i, seq in enumerate(seqs):
        progress(0.7 * i / n, f"Listening to “{seq['name']}” ({i + 1}/{n})…")
        try:
            analyses[seq['path']] = automix.analyze_song(seq, analysis_cache_dir,
                                                         online=online, log=log)
        except Exception as e:
            log(f"    analysis failed for {seq['name']}: {e}")
            continue
        if online and seq['path'] not in mbc:
            todo.append(seq)

    for i, seq in enumerate(todo):
        progress(0.7 + 0.28 * i / max(1, len(todo)),
                 f"Looking up genre for “{seq['name']}” ({i + 1}/{len(todo)})…")
        res = musicbrainz_tags(seq, analyses[seq['path']].get('meta', {}), log)
        if res is not None:
            mbc[seq['path']] = res
            store.save_mb(mbc)

    stats = _lib_stats(list(analyses.values()))
    songs = {}
    for seq in seqs:
        A = analyses.get(seq['path'])
        if not A:
            continue
        cats, why = classify(seq, A, mbc.get(seq['path']), stats)
        title = automix._clean_title(seq['name'].split(' - ')[0])
        q, n_chorus = _mashability(A, title)
        key = A.get('key') or {}
        songs[seq['path']] = {
            'name': seq['name'],
            'auto_categories': cats,
            'reasons': why,
            'bpm': A['bpm'],
            'camelot': key.get('camelot'),
            'key': key.get('name'),
            'features': A.get('features') or {},
            'quality': q,
            'n_chorus': n_chorus,
            'duration_ms': A['duration_ms'],
            'genre_source': (mbc.get(seq['path']) or {}).get('label'),
        }
    lib = {'version': LIBRARY_VERSION, 'songs': songs, 'scanned_at': time.time(),
           'online': online}
    store.save_library(lib)
    progress(1.0, 'Library classified')
    return lib


def view(store):
    """Library with overrides applied, plus category counts."""
    lib = store.library()
    over = store.overrides()
    songs = {}
    counts = Counter()
    for path, s in lib.get('songs', {}).items():
        cats = over.get(path, s['auto_categories'])
        songs[path] = {**s, 'categories': cats, 'overridden': path in over}
        counts.update(cats)
    extra = sorted({c for s in songs.values() for c in s['categories']} - set(ALL_CATEGORIES))
    return {
        'scanned': bool(lib.get('songs')),
        'scanned_at': lib.get('scanned_at'),
        'songs': songs,
        'categories': [{'name': c, 'count': counts[c]} for c in ALL_CATEGORIES + extra],
    }


def set_categories(store, path, categories):
    over = store.overrides()
    lib = store.library()
    auto = (lib.get('songs', {}).get(path) or {}).get('auto_categories')
    cats = [c.strip() for c in categories if c and c.strip()]
    if auto is not None and sorted(cats) == sorted(auto):
        over.pop(path, None)               # back to automatic
    else:
        over[path] = cats
    store.save_overrides(over)


# ═══════════════════════════════════════════════════════════════════════
#  Song picking
# ═══════════════════════════════════════════════════════════════════════

def _compat(a, b):
    tempo = 8 * automix._tempo_diff(a['bpm'], b['bpm'])
    d = automix._camelot_dist(a.get('camelot'), b.get('camelot'))
    key = [0, 0.2, 0.6][d] if d < 3 else 0.6 + 0.3 * (d - 2)
    fa, fb = a.get('features', {}), b.get('features', {})
    bass = 4 * abs(fa.get('bass_ratio', 0.3) - fb.get('bass_ratio', 0.3))
    loud = 0.08 * abs(fa.get('loudness_db', -20) - fb.get('loudness_db', -20))
    return -(tempo + 0.7 * key + bass + loud)


def _gumbel(rng):
    return -math.log(-math.log(rng.random() + 1e-12) + 1e-12)


def pick_songs(lib_view, categories, match_all, count, pinned, excluded,
               history, rng, playable):
    songs = lib_view['songs']
    cats = set(categories)

    def matches(s):
        sc = set(s['categories'])
        if not cats:
            return True
        return cats <= sc if match_all else bool(cats & sc)

    talk_ok = 'Intros & Talking' in cats
    pinned = [p for p in pinned if p in songs]
    pool = [p for p, s in songs.items()
            if p in playable and p not in excluded and p not in pinned and matches(s)
            and (talk_ok or 'Intros & Talking' not in s['categories'])]
    need = max(0, count - len(pinned))

    cat_key = '|'.join(sorted(cats)) + ('&' if match_all else '|')
    past = [h for h in history if h.get('cats') == cat_key]
    past_sets = {frozenset(h['songs']) for h in past}
    recent = Counter(p for h in past[-8:] for p in h['songs'])

    def one_pass():
        sel = list(pinned)
        cand = list(pool)
        while len(sel) < len(pinned) + need and cand:
            def score(p):
                s = songs[p]
                v = s['quality']
                if sel:
                    v += float(np.mean([_compat(songs[q], s) for q in sel]))
                v -= 0.6 * recent[p]
                return v + 0.9 * _gumbel(rng)
            best = max(cand, key=score)
            sel.append(best)
            cand.remove(best)
        return sel

    forced = len(pool) <= need
    sel = one_pass()
    for _ in range(40):
        if forced or frozenset(sel) not in past_sets:
            break
        sel = one_pass()
    return sel, {'pool_size': len(pool), 'forced': forced, 'cat_key': cat_key,
                 'short_by': max(0, need - len(pool))}


def plan_signature(plan):
    return [[c['xsq_path'], round(c['start_ms'] / 1000), round(c['end_ms'] / 1000)]
            for c in plan['clips']]


def generate(store, seq_by_path, analysis_cache_dir, opts, progress=None, log=print):
    """
    opts: categories, match_all, count, pinned, excluded, target_s,
          max_total_s, allow_reuse, online, crossfade_ms
    """
    progress = progress or (lambda f, m: None)
    lv = view(store)
    if not lv['scanned']:
        scan(list(seq_by_path.values()), store, analysis_cache_dir,
             online=opts['online'],
             progress=lambda f, m: progress(0.8 * f, m), log=log)
        lv = view(store)

    progress(0.82, 'Picking songs…')
    rng = random.Random(time.time_ns() ^ random.getrandbits(32))
    history = store.history()
    picked, info = pick_songs(lv, opts['categories'], opts['match_all'], opts['count'],
                              opts['pinned'], set(opts['excluded']), history, rng,
                              set(seq_by_path))
    if not picked:
        raise ValueError('No songs match those categories. Try fewer categories, '
                         '"match any", or re-scan the library.')

    seqs = [seq_by_path[p] for p in picked]
    past_plans = [h.get('plan') for h in history if h.get('plan')]
    plan = None
    for attempt in range(8):
        progress(0.85 + 0.02 * attempt, 'Arranging the mashup…')
        plan = automix.plan_mashup(
            seqs, analysis_cache_dir, target_s=opts['target_s'], allow_reorder=True,
            allow_reuse=opts['allow_reuse'], max_total_s=opts['max_total_s'],
            online=opts['online'], crossfade_ms=opts['crossfade_ms'],
            seed=rng.getrandbits(32), log=log)
        if plan_signature(plan) not in past_plans:
            break

    history.append({'cats': info['cat_key'], 'songs': picked,
                    'plan': plan_signature(plan), 'time': time.time()})
    store.save_history(history)

    songs = lv['songs']
    plan['picked'] = [{
        'xsq_path': p, 'name': songs[p]['name'],
        'categories': songs[p]['categories'],
        'pinned': p in opts['pinned'],
        'bpm': round(songs[p]['bpm']), 'camelot': songs[p].get('camelot'),
    } for p in picked]
    plan['pick_info'] = info
    progress(1.0, 'Done')
    return plan
