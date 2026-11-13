# xLights Mashup Builder — Architecture Document

> **Created:** 2026-11-06 · **Last updated:** 2026-11-08
> **Status:** Working — 79 sequences scanned, all media resolved; builds, Automate, Generate, loudness, video re-timing and ending fade verified on the real show

---

## 1. Overview

A local web app that builds **mashup sequences** from an existing xLights show. You pick songs (or let the app pick them), trim each to a section, order them, and the app writes:

1. A **merged `.xsq` sequence** — every lighting effect time-shifted and index-remapped; Video effects re-timed; optional fade-out at the end
2. A **merged `.mp3`** — clips cut, loudness-levelled, crossfaded, optionally faded out
3. Both together as a **zip** containing a folder named after the mashup

The tool **never modifies** the show folder or media files. Output goes to `output/`.

### Feature summary

| Area | What it does |
|------|--------------|
| Timeline editing | Two-handle trim slider per clip, typed times, drag-to-reorder, drag from library, ⧉ reuse another part of a song, remove |
| Save / Load | Named mashup projects on disk, unsaved-changes tracking, Ctrl+S |
| ✨ Automate | Listens to the songs in the timeline, reads their lyrics, picks cut points on bar lines that never cut a sung line, chooses running order (optionally letting songs come back), respects a max length |
| 🎲 Generate | Classifies the whole library (themes / genres / moods), picks songs by category that work together, arranges them; every generation is unique; respects your pins and removals |
| Build | Loudness normalisation (−16 LUFS), Video effect re-timing, crossfade, ending fade-out (lights + audio), one-click zip download |

---

## 2. Directory Layout

```
xlights-mashup/
│  ── Source (in git) ──────────────────────────────────────────────
├── app.py                     # Flask server: routes, background jobs, config + first-run setup
├── core/                      # Supporting modules (imported by app.py)
│   ├── __init__.py
│   ├── mashup_engine.py       # XSQ parsing/merging, media resolution, ffmpeg,
│   │                          #   loudness, video re-timing, ending fade
│   ├── automix.py             # Audio + lyric analysis, clip candidates, planner (✨ Automate)
│   └── library.py             # Library classification, MusicBrainz, song picker (🎲 Generate)
├── templates/
│   ├── index.html             # Single-page UI (inline CSS + JS, no build step, no CDN)
│   └── help.html              # User guide served at /help (❓ Help in the app header)
├── static/help/*.png          # 20 screenshots used by the help page
├── config.example.json        # Template for config.json
├── install.py                 # Installer: Python check, ensurepip, pip install -r, verify
├── requirements.txt           # flask, imageio-ffmpeg, numpy (major-version capped)
├── README.md, ARCHITECTURE.md
├── .gitignore
│
│  ── Created at run time (git-ignored) ────────────────────────────
├── config.json                # Your show/media paths (written by first-run setup)
├── output/<Name>/             # Built mashups: <Name>.xsq, <Name>.mp3
├── saved_mashups/<Name>.json  # Saved mashup projects
├── library_overrides.json     # Your category corrections
├── analysis_cache/<sha1>.json # Per-song analysis (beats, key, lyrics, energy, features)
├── library_cache/
│   ├── library.json           # Classified library
│   ├── musicbrainz_cache.json # Genre/tag lookups (permanent)
│   └── generator_history.json # Past generations (for uniqueness), last 200
└── loudness_cache.json        # Per-file LUFS / true peak
```

Caches are safe to delete (they rebuild, but a full library re-scan takes a few minutes). `config.json`, `output/`, `saved_mashups/` and `library_overrides.json` are your data.

---

## 3. Configuration (`config.json`)

Show paths are read at startup from `config.json` next to `app.py` (git-ignored; copy `config.example.json`):

```json
{ "show_dir": "<your xLights show folder>", "media_dir": "<your media folder>" }
```

- `SEQ_DIR` = `show_dir\Sequences` unless `"sequences_dir"` is given.
- Environment variables `XLMASHUP_SHOW_DIR` / `XLMASHUP_MEDIA_DIR` override the file.
- **First-run setup** (`ensure_config`, run from `__main__` only): if `config.json` is missing, or its `show_dir` no longer exists, the app shows tkinter folder pickers (show folder — warns if it has no `Sequences`; then media folder, Cancel = show folder) and writes `config.json`. Falls back to console prompts if no window can be shown. Skipped when `XLMASHUP_SHOW_DIR` is set.
- If setup is skipped, the console prints a hint and the library panel shows the error instead of an empty list.
- `OUTPUT_DIR`, `SAVES_DIR`, `ANALYSIS_CACHE_DIR` and the other caches are always next to `app.py`.

Tunable constants live at the top of each module:

| Constant | Where | Value | Meaning |
|----------|-------|-------|---------|
| `LOUDNESS_TARGET_LUFS` | mashup_engine | −16.0 | Loudness every song is brought to |
| `CACHE_VERSION` | automix | 9 | Bump to invalidate `analysis_cache/` after changing analysis code |
| `SR` | automix | 11025 | Analysis sample rate |
| `MAX_USES`, `REUSE_PENALTY`, `THIRD_VISIT_PENALTY`, `PINGPONG_PENALTY`, `BOOKEND_BONUS`, `OVERLAP_TOL`, `BEAM_WIDTH` | automix | 3, 2.0, 1.0, 1.2, 2.0, 500 ms, 250 | Song-reuse planner behaviour |
| `OPEN_BONUS`, `CLOSE_BONUS` | automix | 1.2, 1.2 | Opening on a real intro / closing on a real ending |
| `LIBRARY_VERSION` | library | 2 | Bump to force reclassification |
| `LEXICON`, `TAG_RULES`, `NAME_HINTS` | library | — | Category word lists and MusicBrainz tag mapping |

---

## 4. Dependencies

| Package | Version | Purpose |
|---------|---------|---------|
| Python | 3.15 (tested) | Runtime |
| Flask | 3.1+ | Web server |
| imageio-ffmpeg | 0.8+ | Bundled ffmpeg (no system ffmpeg needed) |
| numpy | 2.x | Audio analysis (STFT, onsets, chroma, features) |

```bash
pip install flask imageio-ffmpeg numpy
```

Online services (optional, no API keys; generic User-Agent, never the user's email):
- **LRCLIB** (`lrclib.net`) — synced lyrics for sequences without a lyric track
- **MusicBrainz** (`musicbrainz.org`) — genre tags; rate-limited to 1 request/second

---

## 5. xLights XSQ File Format

### 5.1 Overall structure

UTF-8 XML, root `<xsequence>`, children in this order:

```xml
<xsequence BaseChannel="0" ChanCtrlBasic="0" ChanCtrlColor="0" FixedPointTiming="1" ModelBlending="true">
  <head>...</head> <nextid>1</nextid> <Jukebox/>
  <ColorPalettes>...</ColorPalettes> <EffectDB>...</EffectDB> <DataLayers>...</DataLayers>
  <DisplayElements>...</DisplayElements> <ElementEffects>...</ElementEffects>
  <lastView>0</lastView> <TimingTags>...</TimingTags>
</xsequence>
```

### 5.2 `<head>`

```xml
<head>
  <version>2022.14</version> <author/> <song/> <artist/> ...
  <sequenceTiming>25 ms</sequenceTiming>     <!-- 25ms = 40fps, some imports 50ms -->
  <sequenceType>Media</sequenceType>
  <mediaFile>D:\...\song.mp3</mediaFile>     <!-- often stale after imports -->
  <sequenceDuration>165</sequenceDuration>   <!-- seconds, may be float -->
</head>
```

`<song>`/`<artist>` are often empty, HTML-double-escaped (`Isn&amp;apos;t`), or hold the *sequence vendor's* name (`LawrenceDrive`, `Showstopper Seq`) — don't trust them as artist.

### 5.3 `<ColorPalettes>` and `<EffectDB>`

Both are **indexed lists**: effects reference them by position (`palette="3"`, `ref="17"`). Each entry is a comma-separated `key=value` string (commas inside values are escaped as `&comma;`). Default values are **omitted**.

| Prefix | Meaning |
|--------|---------|
| `B_` | Buffer / render style |
| `E_` | Effect-specific |
| `T_` | Layer blending and transitions |
| `C_` | Colour (normally in the palette) |

### 5.4 Settings this project reads or writes

All confirmed from this show's own sequences (17,430 EffectDB entries surveyed):

| Key | Values seen | Used for |
|-----|-------------|----------|
| `E_FILEPICKERCTRL_Video_Filename` | absolute, relative or bare filename | Video length lookup |
| `E_TEXTCTRL_Video_Starttime` | seconds, e.g. `0.00`, `19.8`, `33.04` | Video re-timing |
| `E_CHOICE_Video_DurationTreatment` | Normal (206), Loop (180), Normal No Blue (56), Slow/Accelerate (50), Manual and Loop (16), Manual (2) | Video re-timing |
| `E_TEXTCTRL_Video_Speed` | `1.00`, `2`, `.25`, `-10.00` … | Manual treatments |
| `E_CHECKBOX_SynchroniseWithAudio` | `0` in all 510 Video effects | Handled anyway |
| `T_TEXTCTRL_Fadeout` / `T_TEXTCTRL_Fadein` | seconds, e.g. `.5`, `1`, `2` (7,913 / 5,784 entries) | Ending fade |
| `T_CHOICE_Out_Transition_Type` | Wipe, Circle Explode, Swap, Fold… (only written when **not** Fade — Fade is the default) | Removed on faded effects |
| `T_SLIDER_/T_VALUECURVE_/T_CHECKBOX_Out_Transition_*` | adjust / reverse for non-fade transitions | Removed with the type |

Value curve format (seen in `T_VALUECURVE_*`): `Active=TRUE|Id=…|Type=Ramp|Min=0.00|Max=100.00|P1=67.00|P2=33.00|RV=TRUE|` or `Type=Custom|…|Values=0.00:0.30;0.14:0.50;…|` (normalised x:y points).

### 5.5 `<DisplayElements>` and `<ElementEffects>`

```xml
<Element type="model" name="Arches">
  <EffectLayer>
    <Effect ref="0" name="Spirals" startTime="0" endTime="26000" palette="0"/>
    <Effect ref="3" name="Chase" id="1" startTime="26000" endTime="52000" palette="2"/>
  </EffectLayer>
  <SubModelEffectLayer name="Wreath">...</SubModelEffectLayer>
  <Strand index="0"><EffectLayer>...</EffectLayer></Strand>
</Element>
<Element type="timing" name="Lyrics">
  <EffectLayer>   <!-- layer 0: phrases -->
    <Effect label="Hello world" startTime="5450" endTime="6000"/>
  </EffectLayer>
  <EffectLayer>   <!-- layer 1: words -->  </EffectLayer>
  <EffectLayer>   <!-- layer 2: phonemes --> </EffectLayer>
</Element>
```

Times are milliseconds. `id` is sequential per layer (first effect has none).

### 5.6 Timing tracks in this show (survey of 79 sequences)

- **76/78** original sequences have labelled lyric tracks. xLights lyric tracks have **3 layers** (phrases / words / phonemes); some have only 2. Layer 0 is sometimes a single whole-song phrase, empty, or word-level — the analyser detects this and regroups words into lines.
- Many sequences have several singer tracks (`Lead Vocal`, `Chorus`, `Lyrics - Male/Female`, `PEPA`, `Alvin`…).
- Beat tracks (1 layer): `Beats`, `Beat Count` (labels `1..4`), `Bars`, `Metronome`, `Tempo`. Some are marked in half-beats (>180 BPM).
- Some lyric tracks are phonetic (*Feliz Navidad*: "Fay Lease Nav E Dad"; *Carol of the Kings*).

### 5.7 Related files

| File | Purpose |
|------|---------|
| `xlights_rgbeffects.xml` | Layout: models, groups (83 models, 100 groups) |
| `*.fseq` | Rendered frames — must be **re-rendered** in xLights after building |
| `*.xbkp` | xLights backup |

---

## 6. Build Engine (`core/mashup_engine.py`)

### 6.1 Pipeline

```
For each clip (xsq_path, start_ms, end_ms, media_file):
  1. XSQParser                 parse palettes, EffectDB, elements, effects
  2. extract_clip_effects      keep effects overlapping the clip, clip at the
                               boundaries, shift to 0; re-time trimmed Video
                               effects (new EffectDB entries)
  3. cut_audio                 ffmpeg cut of the RESOLVED media file
                               + loudness gain (+ limiter if needed)
                               + afade on the last clip if fading out
Merge:
  4. concat_audio              concat or acrossfade chain (tri curve)
  5. merge_clips_to_xsq        dedupe palettes/EffectDB by text, union display
                               elements, offset times (minus crossfade), remap
                               ref/palette, apply_end_fade, renumber ids, write
```

### 6.2 Key functions

| Function | Purpose |
|----------|---------|
| `scan_sequences` / `SequenceInfo` | Find `.xsq` (skips `import/` subfolders), header parse, media resolution |
| `resolve_media` | 6-step media path resolution (§7) |
| `XSQParser` | Full parse into dicts |
| `extract_clip_effects` | Time-window extraction + Video re-timing |
| `adjust_video_settings` | Video effect re-timing rules (§6.5) |
| `measure_loudness`, `loudness_gain_db` | EBU R128 loudness (§6.6) |
| `cut_audio(…, gain_db, limiter, fade_out_ms)` | ffmpeg segment cut with filters |
| `concat_audio` | Concat / crossfade |
| `apply_end_fade` | Lights fade-out via Fade Out transitions (§6.7) |
| `merge_clips_to_xsq` | Merge and write XSQ; returns number of faded effects |
| `build_mashup(clips, out_dir, name, normalize_audio, fade_out_ms)` | Orchestrator |

### 6.3 Index remapping

Indices collide across sequences, so used palette/EffectDB entries are copied into merged lists, **deduplicated by exact text**, and every `ref`/`palette` rewritten via `(clip_index, old_index) → new_index`.

### 6.4 Boundaries and crossfade

Effects crossing a clip boundary are clipped to it. With crossfade X between N clips: `total = Σ durations − (N−1)·X`, `offset[i] = offset[i−1] + dur[i−1] − X`. Audio uses `acrossfade` (triangle); lights from both clips overlap during the fade.

The same song may appear in several clips — everything is indexed per clip, so repeats are safe.

**Media:** the build uses the **resolved** media path passed from `app.py` (`clip['media_file']`), not the possibly-stale XSQ header path. A missing media file is an error (previously it was silently skipped, which would desync audio and lights).

### 6.5 Video effects — re-timed, never cut

xLights' Video effect stores where in the file to begin (`E_TEXTCTRL_Video_Starttime`) and how effect time maps to video time (`E_CHOICE_Video_DurationTreatment`). When a clip boundary trims a Video effect, its settings are rewritten so the picture stays locked to the music:

| Treatment | Front trimmed by Δ | Only end trimmed |
|-----------|-------------------|------------------|
| Normal / Normal No Blue | `Starttime += Δ` | unchanged |
| Loop | `Starttime = (Starttime + Δ) mod length` | unchanged |
| Manual / Manual and Loop | `Starttime += Δ × Speed` (mod length if looping) | unchanged |
| Slow/Accelerate | → **Manual**, `Speed = (length − Starttime) / original effect duration`, `Starttime += Δ × Speed` | same conversion (stretch would otherwise change speed) |
| Synchronise with audio | unsync, `Starttime = original song position`, treatment → Normal | — |

Video length comes from `ffmpeg -i` (filename resolved with `resolve_media`, cached). Each re-timed effect gets a **new EffectDB entry**; other effects sharing the original entry are untouched. Build result reports `videos_retimed` and notes.

Verified on a build: *It's Raining Tacos* clip at 0:30 → `Starttime 30.000`; *Greatest Show* curtain (Slow/Accelerate, 64.45 s effect, 69.2 s video) trimmed 3 s → `Manual, Speed 1.0740, Starttime 3.222`; Loop trimmed 5 s → `5.000`.

> **Research caveat:** the xLights source (`VideoEffect.cpp`) could not be read during development (web fetch blocked, git not installed). Setting names and units come from this show's 510 Video effects; the per-treatment playback formulas are based on known xLights behaviour. Spot-check a trimmed video in xLights.

20 Video effects reference files that don't exist at the stored path (e.g. `CC0 - Bear.mp4`, `ImportedMedia/…/Snow Angel.mp4`); xLights may still find them via show/media folders. Paths are written to the mashup unchanged.

### 6.6 Loudness normalisation (like mp3gain, using EBU R128)

- `measure_loudness(path)`: `ffmpeg -af ebur128=peak=true` on the **whole file** → integrated LUFS + true peak dBFS. Cached in `loudness_cache.json` (key: path + mtime + size). ~0.4 s per song.
- Gain per clip = `−16 − song LUFS`, clamped to −20…+12 dB. Measured on the whole song, so a song's quiet verse stays quieter than its chorus.
- If `true peak + gain > −1 dBTP`, add `alimiter=limit=0.891:level=0:latency=1`. **`level=0` is essential** — ffmpeg's alimiter auto-levels by default, which would push every clip up to the limit.
- Measured spread in this show before levelling: −11.4 LUFS (*Sleigh Ride*) to −19.2 LUFS (*Frosty*).
- Build option `normalize_audio` (UI: "Even out loudness", default on). Result reports `gains_db` per clip.

### 6.7 Ending fade-out

Build option `fade_out_ms` (UI: "Fade out ending", default on, 3 s, 0.5–15 s).

**Lights** — `apply_end_fade` uses xLights' per-effect **Fade Out** transition on model effects (timing tracks untouched) overlapping the last `F` ms:
- effects reaching the end: `T_TEXTCTRL_Fadeout = min(F, effect length)` → black exactly at the end
- effects ending inside the window: fade-out = time from window start to their end → reach black when they end instead of cutting off at full brightness
- `T_CHOICE_Out_Transition_Type` and its adjust/reverse keys are removed (Fade is the default type); an existing longer plain fade-out is kept; fade-in is shortened if fade-in + fade-out > effect length
- changed settings get new EffectDB entries

**Audio** — `afade=t=out` on the last segment. Because `cut_audio` uses `-ss` as an *output* option, filter timestamps are in **source-file time**, so `st = start_s + duration_s − F`. (Getting this wrong silences the whole last clip — it happened once during development.)

The window is capped to fit inside the last clip, clear of its crossfade. Verified: 130 effects faded; audio −17 → −21 → −29 → −46 dB over the last 3 s.

Limitation: an effect that *starts* inside the window begins at full brightness, then fades (xLights fades only ramp to/from zero).

---

## 7. Media Resolution

`resolve_media` tries, in order: exact path → exact filename in media index → normalised stem (strip `"03 - "`, punctuation) → substring stem match (same extension) → sequence-name folder with exactly one audio file → parent folder of `Music/`/`Audio/` in the stale path. The index is built once over `MEDIA_DIR` and `SHOW_DIR`. Currently 79/79 sequences resolve (all original paths exist).

---

## 8. ✨ Automate (`core/automix.py`)

### 8.1 Per-song analysis (`analyze_song`, cached)

Cache key: `CACHE_VERSION` + xsq path + media path + both mtimes. First analysis ≈ 0.3–0.9 s per song.

| Signal | Source (best first) |
|--------|---------------------|
| **Lyrics** | Sequence lyric track → LRCLIB synced lyrics (duration ±5 s; also reports "instrumental") → none (audio structure) |
| **Beats** | Sequence `Beats`/`Beat Count`/`Metronome` track → audio beat tracking (spectral-flux onsets, autocorrelation tempo with a ~115 BPM prior, ±1.5 % tempo + phase search). Tracks >180 BPM are halved |
| **Downbeats** | `Bars` track → beat label `1` → audio (strongest low-band onset phase in 4/4) |
| **Key** | Chroma (55–2000 Hz) + Krumhansl profiles → name + Camelot code |
| **Energy** | dBFS per 100 ms (≈1 s smoothing) |
| **Sections (instrumental)** | Bar-level chroma + loudness novelty peaks, ≥ 8 bars apart |
| **Features** | bass ratio (<150 Hz), spectral centroid, onset density (hits/s), dynamics, median loudness — used by the library |

**Lyric track choice** (`_sequence_lyrics`):
- Lyric track = ≥ 2 layers and ≥ 10 labelled items. Lines come from layer 0 if it looks like phrases, else words are grouped (650 ms gap / 9 s max).
- **Copied-track detection:** every lyric track in the show is fingerprinted (first 80 word labels + times). A track is ignored when the identical track exists in another sequence **and** it doesn't sing this song's title — unless it was copied out together with this song's own title-singing track (then it's ours). Duet / backing tracks unique to a sequence are always kept. `set_sequence_paths()` (called from `app.get_sequences`) enables this.
- Main lyrics = biggest track that sings the title (if it's ≥ 30 % of the largest), else the biggest track. All kept tracks form the "vocal activity" used to avoid cutting words.

> **Finding in the development show:** five template-built sequences carry other songs' lyric tracks (apparently from a shared template): *We Don't Talk About Bruno* (tracks from 5 other songs), *Witch Doctor*, *Rudolph*, *Christmas Without You*, *Christmastime Again*. Harmless in xLights but they previously made *Bruno* look like a Christmas song.

### 8.2 Structure and candidate clips (`Song`)

- Lines that repeat ≥ 12 s apart (exact or ≥ 70 % token overlap) → chorus. Lines group into blocks split on vocal gaps (≥ 1.6 bars / 2.5 s) or chorus/verse changes; blocks > 32 s are split at the biggest internal gap.
- Section labels: a new number when the type changes, after an instrumental break, or when the chorus's opening line comes round again.
- Starts snap to the downbeat before a block's first line; ends to the first downbeat after its last line (or just before the next vocal if that downbeat would clip it). Optional *opening* (song start) and *closing* (song end) variants.
- Segment score: contains a chorus (+3), ends on a chorus (+1.2), verse→chorus build (+0.6), title hook (+0.8), energy z-score, length vs target (`−5·ln(T/d)` if short, `−4·ln(d/T)` if long; window 0.6–1.7 × target), slight preference for earlier occurrences, penalties for clipping sung words or off-grid cuts. Up to 8 diverse candidates per song (+ best opening/closing). Optional random jitter (seed) for Generate.

### 8.3 Planner (`_search`, beam search)

Slots = (song, candidate). Transition cost: tempo `8·min|ln ratio|` (allowing ½× / 2×), Camelot distance, loudness jump > 3 dB, vocal-into-vocal clash.

- Every song appears at least as many times as the user added it.
- With **reuse**, a song can return (max 3 visits), never back-to-back, never replaying a part already used (>500 ms overlap); extra visits cost 2.0 (+1.0 for a third), A→B→A costs 1.2, opening and closing on the same song earns +2.0.
- **Max length** is a hard cap; time is reserved for songs not yet placed so nothing gets squeezed out if they can all fit. If `n × target > 0.9 × max`, target shrinks to `max/n × 0.85` (min 8 s). Songs that still can't fit are reported as dropped.
- Soft total = `slots × target × (1.5 with reuse)`; exceeding it costs `3 × excess/target`.
- **Order modes** (`order_mode`): `best` — the beam search picks the order for the smoothest flow (tempo, key, energy) and may bring songs back; `scramble` — `_scramble` shuffles the clip slots (different every run, avoids the same song back-to-back and the current order), then the search only picks cuts; `keep` — the user's order, cuts only. Song returns apply to `best` only. Legacy `allow_reorder=false` maps to `keep`.
- Auto crossfade ≈ 2 beats (median), 400–1500 ms. With crossfade X, internal cuts move by X/2 so the fade is centred on the bar line.

Typical runtime: 0.2–0.3 s for 6 songs once analysed.

---

## 9. Library and 🎲 Generate (`core/library.py`)

### 9.1 Classification (`scan`)

Incremental; first full scan of 79 songs ≈ 4–5 min (MusicBrainz 1 req/s), later rescans seconds. Categories:

| Group | Categories | Source |
|-------|-----------|--------|
| Themes | Christmas, Halloween, Faith, Comedy, Movie & Musical, Kids, Intros & Talking | Weighted word lists over lyrics + title words; name hints (`(Comedy)`, `Talking`…); MusicBrainz tags |
| Genres | Pop, Rock, Country, Dance, Hip-Hop & R&B, Jazz & Classics, Orchestral, A Cappella | MusicBrainz recording tags + artist genres (top 6 only — long tails over-tag) |
| Moods | Upbeat, Ballad / Slow, Bass-heavy, Instrumental | Audio features relative to the library (onset density and loudness first; tempo second because it can be off by 2×) |

Rules worth knowing: Christmas needs a strong word in the title, or ≥ 1 strong word and density ≥ 3/100 words; Faith needs ≥ 2 *distinct* strong words; "pumpkin" is only a weak Halloween word (pumpkin pie). User corrections (✎ tags) are stored in `library_overrides.json` and win over automatic results; "Back to automatic" removes the override.

Current counts: Christmas 48, Upbeat 45, Pop 30, Rock 22, Bass-heavy 20, Ballad 17, Dance 11, Faith 10, Hip-Hop & R&B 9, Orchestral 8, Country 6, Jazz & Classics 6, Intros & Talking 4, Instrumental 4, A Cappella 3, Comedy 2, Movie & Musical 2, Halloween 1, Kids 1.

Each song also gets a *mashability* score (has lyrics, repeated chorus, length ≥ 70 s).

### 9.2 Picking songs (`pick_songs`, `generate`)

1. Pool = songs matching the chosen categories (**all** or **any**), minus songs the user removed, minus `Intros & Talking` unless chosen.
2. Songs the user added themselves (pinned) are always included.
3. Remaining picks are added one at a time by `mashability + mean compatibility with picks so far (tempo, key, bass ratio, loudness) − 0.6 × recent use + 0.9 × Gumbel noise`.
4. A set already generated for the same category selection is rejected (up to 40 retries) using `generator_history.json`.
5. The picks go through the Automate planner with a random seed; if the pool forces the same songs, the plan is re-rolled (up to 8 seeds) until its order/cuts differ from past plans.

Verified: identical settings produced different sets; pins kept; removals honoured; a 3-song category produced a different arrangement on the second run.

---

## 10. Flask API (`app.py`)

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/` | GET | UI |
| `/help` | GET | User guide with screenshots |
| `/api/sequences` | GET | All sequences + metadata |
| `/api/build_mashup` | POST | Build (sync). Body below |
| `/api/download/<path>` | GET | One file from `output/` |
| `/api/download_all/<name>` | GET | Zip of `output/<name>/` as `<name>/…` (mp3 stored, xsq deflated; name must be a sanitised mashup name) |
| `/api/mashups` | GET | List saved projects (newest first) |
| `/api/mashups` | POST | Save project; `409 {"error":"exists"}` unless `overwrite` |
| `/api/mashups/<id>` | GET / DELETE | Load / delete a project |
| `/api/automate` | POST | Start Automate job → `{job_id}` |
| `/api/library` | GET | Library view (categories applied, counts) |
| `/api/library/scan` | POST | Start classification job `{online}` |
| `/api/library/categories` | POST | Set a song's categories `{path, categories}` |
| `/api/generate` | POST | Start Generate job |
| `/api/jobs/<id>` (alias `/api/automate/<id>`) | GET | `{status, progress, message, result, error}` |

Long tasks run in background threads; library scan and generate share a lock (one at a time). Finished jobs are forgotten after an hour. Only sequence paths known to `get_sequences()` are accepted.

### Request bodies

```jsonc
// POST /api/build_mashup
{ "name": "MyMashup", "crossfade_ms": 900, "normalize_audio": true, "fade_out_ms": 3000,
  "clips": [{ "xsq_path": "<show>\\Sequences\\Let It Go\\Let It Go.xsq", "start_ms": 45000, "end_ms": 88000 }] }
// → result: xsq_path, audio_path, duration_ms, num_clips, download_zip, download_xsq, download_audio,
//           gains_db[], normalized, videos_retimed, video_notes[], fade_out_ms, effects_faded

// POST /api/automate
{ "clips": [{ "xsq_path": "..." }], "target_s": 30, "order_mode": "best|scramble|keep", "allow_reuse": true,
  "max_total_s": 300, "online": true, "crossfade_ms": null /* null = auto */ }
// → plan: clips[{xsq_path, name, visit, start_ms, end_ms, note, bpm, key, camelot, clean_cuts,
//          lyrics_source, beat_source}], transitions[{text, verdict}], crossfade_ms, crossfade_auto,
//          reordered, order_mode, total_ms, max_ms, target_used_s, target_shrunk, dropped[], all_fit, revisits

// POST /api/generate  (plan as above + picked[{xsq_path, name, categories, pinned, bpm, camelot}], pick_info)
{ "categories": ["Christmas", "Upbeat"], "match_all": true, "count": 6, "pinned": [], "excluded": [],
  "target_s": 25, "max_total_s": 300, "allow_reuse": true, "online": true, "crossfade_ms": null }
```

### Saved project (`saved_mashups/<id>.json`)

```jsonc
{ "version": 1, "name": "...", "saved_at": 1794193531.8, "crossfade_ms": 900, "max_length_ms": 300000,
  "normalize_audio": true, "fade_out_ms": 3000, "excluded": ["...xsq"],
  "clips": [{ "xsq_path": "...", "seq_name": "...", "start_ms": 0, "end_ms": 30000,
              "note": "Verse 1 → Chorus 1 · …", "origin": "user|generated" }] }
```

`id` = sanitised name (letters, numbers, space, `-`, `_`). Saves are written atomically (temp file + replace). Older saves without `fade_out_ms` load with the 3 s default.

---

## 11. Web UI (`templates/index.html`)

Single file, inline CSS/JS, dark theme. Colours are CSS variables on `:root`.

**Left panel — library:** search (name, folder, category), category filter, cards with category chips (green = theme, purple = genre, gold = mood; hover shows why), **✎ tags** editor, **+ Add**, drag a card into the timeline.

**Header:** name, crossfade (ms), **Max length** (`5:00` or `5` = minutes), ➕ New / 📂 Load / 💾 Save (Ctrl+S), "● unsaved changes / ✓ saved".

**Timeline clip cards:** ⠿ drag handle (also ↑/↓ when focused), ⧉ use another part of this song, ✕ remove, two-handle trim slider (green start / red end, 10 ms steps, ≥ 100 ms clip, last-moved handle on top), editable Start/End, duration, ✨ note from Automate/Generate (cleared when you trim by hand).

**Bottom bar:** clip count, total (with "/ max" or red "over by"), Even out loudness, Fade out ending (seconds), 🎲 Generate, ✨ Automate, 🔨 Build.

**Dialogs:** Load (list with clip names, delete), Automate (options incl. running order 🎯 Best flow / 🔀 Scramble / 📌 Keep my order → progress → preview, with *Scramble again* in scramble mode → Apply), Generate (first-run library scan → category chips with live match count, count, length, max, reuse, pinned/excluded lists → preview with picked songs → *Generate another* / Apply), Edit categories, Build (progress → loudness/video/fade summary → **📦 Download all (.zip)**).

**State rules:**
- Clips have `origin`: `user` (added by you — pinned for Generate) or `generated`.
- Removing the last clip of a generated song adds it to the session's *excluded* set (saved with the project); adding it back by hand removes it.
- Automate/Generate never change the timeline until **Apply**.
- `beforeunload` warns about unsaved work.
- `localStorage` (per browser, best-effort) remembers Automate/Generate dialog options only.

Time input formats: `M:SS.mmm`, `M:SS`, `SS.mmm`, plain number (> 999 = ms, else seconds).

---

## 12. Show Folder Structure

```
<show folder>\                                 # SHOW_DIR
├── xlights_rgbeffects.xml, layout.xml, xlights_networks.xml
├── Sequences\                                 # SEQ_DIR
│   ├── <Song A>\<Song A>.xsq
│   ├── <Song B>\<Song B>.xsq + import\        # import\ subfolders are skipped
│   ├── <Mashup>\<Mashup>.xsq                  # mashups copied back in are scanned too
│   └── …
<media folder>\                                # MEDIA_DIR: root files + per-song folders
```

The scanner walks every subfolder of `Sequences`, so any folder layout works. `import\` subfolders (duplicate purchased imports) are skipped. Media filenames often differ from the XSQ reference — hence the resolver (§7). Mashups copied into `Sequences` are scanned like any other sequence.

## 13. Models and Layouts

Effects are copied by model / group / sub-model **name**, together with their display elements. A mashup therefore renders correctly on the show whose layout the source sequences were made for; mixing sequences made for different layouts carries over effects for models that may not exist (xLights ignores those).

---

## 14. Known Limitations

1. **Value curves and fades on trimmed effects** — value curves span an effect's whole length, so a trimmed effect plays its curve compressed; an existing fade-in/out moves to the cut point.
2. **Faces / timing tracks** — timing tracks are time-shifted with their clip; a Faces effect that references a timing track name used by a different clip may not line up.
3. **Ending fade** — effects starting inside the fade window start at full brightness.
4. **Tempo** — no time-stretching; mismatched tempos show as "bold" transitions. Detected tempo can be off by 2× (only > 180 BPM is corrected).
5. **Key detection** is an estimate; **phonetic lyric tracks** read oddly in notes.
6. **Classification** depends on lyric quality and MusicBrainz matches (e.g. *Bruno* isn't tagged Movie & Musical; *The Weekend* misspelling matched an artist with no genres). Use ✎ tags.
7. **Video formulas** not verified against xLights source (§6.5).
8. **Rendering** — `.fseq` still has to be rendered in xLights.
9. **No in-browser audio preview or waveform.**

## 15. Ideas for Later

- In-browser preview of clip ranges / waveform with lyric and section markers on the slider
- Tempo-matching transitions (ffmpeg `atempo`) and key-aware pitch shifting
- Optional LLM pass over lyrics for theme-aware cut choices and categories
- Optional fade-in at the start; per-transition crossfade lengths
- Value-curve remapping for trimmed effects
- xLights xlDo / REST API (port 49913) to open, render and save the mashup automatically

---

## 16. How to Run

```bash
cd xlights-mashup
python install.py            # or: pip install -r requirements.txt   (--check = report only)
python app.py               # opens http://localhost:5000 in your default browser once the server is up
python app.py --no-browser  # start without opening a browser
# restart after changing Python files; hard refresh (Ctrl+F5) after HTML changes
```

If the app is already running on port 5000, launching it again just opens a browser tab to the running copy and exits (instead of a "port in use" error).

First use of 🎲 Generate offers a one-time library scan (≈ 4–5 min with online lookups).

### After building

1. Click **📦 Download all (.zip)** and unzip the folder into `Sequences/` (or copy `output/<Name>/`)
2. Open the `.xsq` in xLights (point it at the `.mp3` in the same folder if asked)
3. Spot-check trimmed videos and the ending fade
4. Render All (Ctrl+Shift+R) to create the `.fseq`, then add to your playlist

---

## 17. Change History

| Date | Change |
|------|--------|
| 2026-11-06 | Initial build: library, timeline with typed times, merge engine, crossfade, media resolver |
| 2026-11-08 | Fixed: second clip couldn't be added / clips couldn't be removed (detached empty-state element); search added the wrong song; quotes in names |
| 2026-11-08 | Two-handle trim slider; drag-and-drop reordering and drag from library |
| 2026-11-08 | Save / Load / New projects, unsaved tracking |
| 2026-11-08 | ✨ Automate (lyrics, beats, key, energy, chorus detection, ordering) |
| 2026-11-08 | Songs can appear several times (⧉ and Automate reuse); max mashup length |
| 2026-11-08 | Fixed: build used stale XSQ media path and silently dropped missing audio |
| 2026-11-08 | Loudness normalisation; Video effect re-timing; library classification + MusicBrainz; 🎲 Generate; copied-lyric-track detection |
| 2026-11-08 | Download all as zip |
| 2026-11-08 | Ending fade-out (lights via Fade Out transitions + audio afade) |
| 2026-11-12 | Automate running-order choice: Best flow / Scramble / Keep my order |
| 2026-11-12 | `app.py` opens the browser automatically when the server is ready (`--no-browser` to skip); a second launch reuses the running copy |
| 2026-11-12 | Show paths moved to git-ignored `config.json` with first-launch folder pickers; build window shows the output path relative to the app folder |
| 2026-11-12 | Cleanup: removed old ad-hoc scripts (`quick_check.py`, `test_resolve.py`) and test builds |
| 2026-11-12 | `install.py`: standard-library installer (version check, ensurepip, pip install -r requirements.txt with `--user` fallback, import + ffmpeg verification, `--check` mode) |
| 2026-11-12 | Supporting modules moved to the `core/` package (`app.py` stays the entry point); loudness cache stays in the app folder |
| 2026-11-12 | Help page (`/help`) with 20 screenshots captured from the running app (Playwright driving Edge, headless). Re-take screenshots after visible UI changes so the guide stays accurate |
