"""
xLights Mashup Engine
=====================
Core logic for parsing XSQ sequence files, extracting time-ranged clips,
merging effects from multiple clips, and cutting/concatenating audio.

XSQ File Structure (XML):
  <xsequence>
    <head>              - Metadata (version, timing, media path, duration)
    <nextid>            - Internal counter
    <Jukebox>           - Jukebox configuration
    <ColorPalettes>     - Indexed list of color palette definitions
    <EffectDB>          - Indexed list of deduplicated effect parameter strings
    <DataLayers>        - Render layer configuration
    <DisplayElements>   - List of models/groups/timing tracks in the sequence
    <ElementEffects>    - Timeline data: effects placed on elements across time
      <Element type="model|timing" name="...">
        <EffectLayer>         - Main effect layers
          <Effect ref="N" name="Type" startTime="ms" endTime="ms" palette="N" />
        <SubModelEffectLayer> - Sub-model specific effects
        <Strand>              - Strand-level effects
    <lastView>
    <TimingTags>        - Bookmark/tag positions

Effects reference EffectDB entries via 'ref' (index) and ColorPalettes via 'palette' (index).
All times are in milliseconds.
"""

import xml.etree.ElementTree as ET
import os
import glob
import subprocess
import shutil
import json
import re
from pathlib import Path

# Get ffmpeg path from imageio_ffmpeg
import imageio_ffmpeg
FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()


# ─── Media Resolution ──────────────────────────────────────────────────

# Global media index: populated once by build_media_index()
_media_index = None        # filename.lower() -> full path
_media_stems_index = None  # normalized stem -> full path


def _normalize_stem(filename):
    """
    Strip track numbers, extra punctuation, and whitespace for fuzzy matching.
    '09 - Into the Unknown (Panic! At The Disco Version).mp3' -> 'into the unknown panic at the disco version'
    """
    stem = os.path.splitext(filename)[0]
    # Strip leading track numbers like "01 - ", "08 - "
    stem = re.sub(r'^\d+\s*[-–.]\s*', '', stem)
    # Lowercase, remove non-alphanumeric (keep spaces)
    stem = re.sub(r'[^a-z0-9\s]', '', stem.lower())
    # Collapse whitespace
    stem = re.sub(r'\s+', ' ', stem).strip()
    return stem


def build_media_index(media_dir, show_dir):
    """
    Build a lookup index of all audio/video files under media_dir and show_dir.
    Returns (exact_index, stems_index).
    """
    global _media_index, _media_stems_index
    exact = {}
    stems = {}
    audio_video_exts = {'.mp3', '.m4a', '.mp4', '.wav', '.ogg', '.flac',
                        '.mov', '.avi', '.mkv'}

    for search_dir in [media_dir, show_dir]:
        if not search_dir or not os.path.isdir(search_dir):
            continue
        for root_dir, dirs, files in os.walk(search_dir):
            for f in files:
                ext = os.path.splitext(f)[1].lower()
                if ext in audio_video_exts:
                    full_path = os.path.join(root_dir, f)
                    key = f.lower()
                    if key not in exact:
                        exact[key] = full_path
                    norm = _normalize_stem(f)
                    if norm and norm not in stems:
                        stems[norm] = full_path

    _media_index = exact
    _media_stems_index = stems
    return exact, stems


def _find_sole_audio_in_folder(folder_name, media_dir):
    """
    If a folder matching folder_name exists in media_dir and contains
    exactly one audio file, return it. Handles cases where the filename
    inside the folder is completely different from the XSQ reference.
    """
    if not media_dir or not os.path.isdir(media_dir):
        return None
    audio_exts = {'.mp3', '.m4a', '.wav', '.ogg', '.flac'}

    # Try to find a matching subfolder
    folder_norm = _normalize_stem(folder_name)
    for d in os.listdir(media_dir):
        dp = os.path.join(media_dir, d)
        if not os.path.isdir(dp):
            continue
        if _normalize_stem(d) == folder_norm or d.lower() == folder_name.lower():
            # Found matching folder — look for audio files
            audio_files = [
                os.path.join(dp, f) for f in os.listdir(dp)
                if os.path.splitext(f)[1].lower() in audio_exts
            ]
            if len(audio_files) == 1:
                return audio_files[0]
    return None


def resolve_media(media_path, media_dir=None, show_dir=None,
                   seq_name=None):
    """
    Resolve a media file path. Tries in order:
    1. Exact path as stored in XSQ
    2. Exact filename match in media/show directories
    3. Fuzzy stem match (strip track numbers, punctuation)
    4. Partial substring match on normalized stems
    5. If seq_name given, look for a folder matching the sequence name
       in media_dir that contains exactly one audio file
    Returns the resolved path, or the original if nothing found.
    """
    global _media_index, _media_stems_index

    if not media_path:
        return media_path

    # 1. Exact path
    if os.path.exists(media_path):
        return media_path

    # Build index if needed
    if _media_index is None:
        if media_dir or show_dir:
            build_media_index(media_dir or '', show_dir or '')
        else:
            return media_path

    basename = os.path.basename(media_path)

    # 2. Exact filename match
    key = basename.lower()
    if key in _media_index:
        return _media_index[key]

    # 3. Fuzzy stem match
    norm = _normalize_stem(basename)
    if norm and _media_stems_index:
        if norm in _media_stems_index:
            return _media_stems_index[norm]

        # 4. Partial substring matching
        orig_ext = os.path.splitext(basename)[1].lower()
        best_match = None
        best_len = 0
        for idx_norm, idx_path in _media_stems_index.items():
            idx_ext = os.path.splitext(idx_path)[1].lower()
            if orig_ext != idx_ext:
                continue
            if norm in idx_norm or idx_norm in norm:
                match_len = min(len(norm), len(idx_norm))
                if match_len > best_len:
                    best_len = match_len
                    best_match = idx_path
        if best_match:
            return best_match

    # 5. Folder-based search using sequence name
    if seq_name and media_dir:
        result = _find_sole_audio_in_folder(seq_name, media_dir)
        if result:
            return result

    # 6. Try stripping common import subfolders (Music/, Audio Files/)
    #    and looking in the parent folder within media_dir
    if media_dir:
        # Extract the parent folder name from the original path
        parts = media_path.replace('\\', '/').split('/')
        # Look for known subfolder patterns
        for skip_folder in ['Music', 'Audio Files', 'Audio']:
            if skip_folder in parts:
                idx = parts.index(skip_folder)
                if idx > 0:
                    parent_name = parts[idx - 1]
                    result = _find_sole_audio_in_folder(
                        parent_name, media_dir)
                    if result:
                        return result

    return media_path


class SequenceInfo:
    """Lightweight metadata about a sequence (header-only parse)."""

    def __init__(self, xsq_path):
        self.path = xsq_path
        self.folder = os.path.dirname(xsq_path)
        self.filename = os.path.basename(xsq_path)
        self.name = os.path.splitext(self.filename)[0]
        self.parent_folder = os.path.basename(self.folder)

        # Parse header only
        tree = ET.parse(xsq_path)
        root = tree.getroot()
        head = root.find('./head')

        self.version = head.findtext('version', '')
        timing_text = head.findtext('sequenceTiming', '25 ms')
        self.timing_ms = int(re.search(r'(\d+)', timing_text).group(1))
        self.seq_type = head.findtext('sequenceType', 'Media')
        self.media_file_original = head.findtext('mediaFile', '') or ''
        self.media_file = self.media_file_original
        self.duration_s = float(head.findtext('sequenceDuration', '0'))
        self.duration_ms = int(self.duration_s * 1000)

        # Count models and effects for display
        self.model_count = len(root.findall('./DisplayElements/Element[@type="model"]'))
        self.effect_count = len(root.findall('./EffectDB/Effect'))

    def resolve(self, media_dir=None, show_dir=None):
        """Attempt to resolve a missing media file."""
        self.media_file = resolve_media(
            self.media_file_original, media_dir, show_dir,
            seq_name=self.name)
        return self

    def to_dict(self):
        media_exists = os.path.exists(self.media_file) if self.media_file else False
        was_resolved = (self.media_file != self.media_file_original
                        and media_exists)
        return {
            'path': self.path,
            'folder': self.parent_folder,
            'filename': self.filename,
            'name': self.name,
            'timing_ms': self.timing_ms,
            'seq_type': self.seq_type,
            'media_file': self.media_file,
            'media_file_original': self.media_file_original,
            'media_exists': media_exists,
            'media_resolved': was_resolved,
            'duration_s': round(self.duration_s, 3),
            'duration_ms': self.duration_ms,
            'model_count': self.model_count,
            'effect_count': self.effect_count,
        }


def scan_sequences(seq_dir, media_dir=None, show_dir=None):
    """Scan for all .xsq files under seq_dir and return their metadata."""
    # Pre-build media index for resolution
    if media_dir or show_dir:
        build_media_index(media_dir or '', show_dir or '')

    xsq_files = glob.glob(os.path.join(seq_dir, "**", "*.xsq"), recursive=True)
    # Filter out sequences in /import/ subfolders
    xsq_files = [f for f in xsq_files
                 if 'import' not in f.replace('\\', '/').lower().split('/')[:-1]]
    sequences = []
    for f in sorted(xsq_files):
        try:
            info = SequenceInfo(f)
            # Only include sequences with media and positive duration
            if info.media_file_original and info.duration_s > 0:
                info.resolve(media_dir, show_dir)
                sequences.append(info)
        except Exception as e:
            print(f"Warning: Could not parse {f}: {e}")
    return sequences


class XSQParser:
    """Full parser for XSQ sequence files."""

    def __init__(self, xsq_path):
        self.path = xsq_path
        self.tree = ET.parse(xsq_path)
        self.root = self.tree.getroot()

        # Parse head
        head = self.root.find('./head')
        timing_text = head.findtext('sequenceTiming', '25 ms')
        self.timing_ms = int(re.search(r'(\d+)', timing_text).group(1))
        self.media_file = head.findtext('mediaFile', '') or ''
        self.duration_s = float(head.findtext('sequenceDuration', '0'))
        self.duration_ms = int(self.duration_s * 1000)

        # Parse color palettes (indexed list - position = index)
        self.color_palettes = []
        for cp in self.root.findall('./ColorPalettes/ColorPalette'):
            self.color_palettes.append(cp.text or '')

        # Parse effect database (indexed list - position = ref index)
        self.effect_db = []
        for eff in self.root.findall('./EffectDB/Effect'):
            self.effect_db.append(eff.text or '')

        # Parse display elements
        self.display_elements = []
        for de in self.root.findall('./DisplayElements/Element'):
            self.display_elements.append(dict(de.attrib))

        # Parse element effects (the main timeline data)
        self.element_effects = self._parse_element_effects()

    def _parse_element_effects(self):
        """Parse the ElementEffects section into a structured list."""
        elements = []
        for ee in self.root.findall('./ElementEffects/Element'):
            elem = {
                'type': ee.attrib.get('type', ''),
                'name': ee.attrib.get('name', ''),
                'layers': [],
                'submodels': [],
                'strands': [],
            }

            for child in ee:
                if child.tag == 'EffectLayer':
                    layer = {'effects': []}
                    for eff in child.findall('./Effect'):
                        layer['effects'].append(dict(eff.attrib))
                    elem['layers'].append(layer)

                elif child.tag == 'SubModelEffectLayer':
                    sub = {
                        'name': child.attrib.get('name', ''),
                        'effects': []
                    }
                    for eff in child.findall('./Effect'):
                        sub['effects'].append(dict(eff.attrib))
                    elem['submodels'].append(sub)

                elif child.tag == 'Strand':
                    strand = {
                        'index': child.attrib.get('index', ''),
                        'layers': []
                    }
                    for layer in child.findall('./EffectLayer'):
                        sl = {'effects': []}
                        for eff in layer.findall('./Effect'):
                            sl['effects'].append(dict(eff.attrib))
                        strand['layers'].append(sl)
                    elem['strands'].append(strand)

            elements.append(elem)
        return elements


# ─── Video effect trimming ─────────────────────────────────────────────
#
# xLights' Video effect does not need the video file cut: the effect
# stores where in the file to begin (E_TEXTCTRL_Video_Starttime, seconds)
# and how to map effect time to video time
# (E_CHOICE_Video_DurationTreatment). When a clip boundary trims the
# front of a Video effect we move that start time forward by the amount
# trimmed, so the frames stay locked to the music.

_video_len_cache = {}


def _probe_duration_s(path):
    if path in _video_len_cache:
        return _video_len_cache[path]
    r = subprocess.run([FFMPEG, '-hide_banner', '-i', path], capture_output=True,
                       text=True, encoding='utf-8', errors='replace')
    m = re.search(r'Duration:\s*(\d+):(\d+):([\d.]+)', r.stderr)
    dur = (int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
           if m else None)
    _video_len_cache[path] = dur
    return dur


def _video_length(filename_setting):
    """Length (s) of a Video effect's file, resolving stale/relative paths."""
    if not filename_setting:
        return None
    p = filename_setting.replace('&comma;', ',')
    if not os.path.exists(p):
        p = resolve_media(p)
    return _probe_duration_s(p) if p and os.path.exists(p) else None


def _setting(tokens, key):
    pre = key + '='
    for t in tokens:
        if t.startswith(pre):
            return t[len(pre):]
    return None


def _put_setting(tokens, key, value):
    pre = key + '='
    for i, t in enumerate(tokens):
        if t.startswith(pre):
            tokens[i] = pre + value
            return
    tokens.append(pre + value)


def adjust_video_settings(text, eff_st, eff_et, new_st, new_et):
    """
    Re-time a Video effect whose original span [eff_st, eff_et] (ms) is
    trimmed to [new_st, new_et]. Returns (new_settings_text, note) or
    (None, None) when nothing needs to change.
    """
    head = max(0, new_st - eff_st)          # ms cut off the front
    tail = max(0, eff_et - new_et)          # ms cut off the end
    if head == 0 and tail == 0:
        return None, None
    tokens = text.split(',')
    treat = _setting(tokens, 'E_CHOICE_Video_DurationTreatment') or 'Normal'
    sync = _setting(tokens, 'E_CHECKBOX_SynchroniseWithAudio') == '1'
    try:
        start = float(_setting(tokens, 'E_TEXTCTRL_Video_Starttime') or 0)
    except ValueError:
        start = 0.0
    try:
        speed = float(_setting(tokens, 'E_TEXTCTRL_Video_Speed') or 1)
    except ValueError:
        speed = 1.0
    fn = _setting(tokens, 'E_FILEPICKERCTRL_Video_Filename')

    if sync:
        # Synced to the original song position, which no longer matches the
        # mashup timeline: unsync and start where the song position was.
        _put_setting(tokens, 'E_CHECKBOX_SynchroniseWithAudio', '0')
        _put_setting(tokens, 'E_TEXTCTRL_Video_Starttime', f'{new_st / 1000:.3f}')
        if treat not in ('Normal', 'Normal No Blue'):
            _put_setting(tokens, 'E_CHOICE_Video_DurationTreatment', 'Normal')
        return ','.join(tokens), 'audio-synced video unsynced and re-timed'

    if treat == 'Slow/Accelerate':
        # The whole video is stretched over the effect; trimming either end
        # would change the speed. Convert to Manual at the same speed.
        length = _video_length(fn)
        if not length or length <= start:
            return None, f'could not read video length for {os.path.basename(fn or "?")}'
        rate = (length - start) / ((eff_et - eff_st) / 1000.0)
        _put_setting(tokens, 'E_CHOICE_Video_DurationTreatment', 'Manual')
        _put_setting(tokens, 'E_TEXTCTRL_Video_Speed', f'{rate:.4f}')
        _put_setting(tokens, 'E_TEXTCTRL_Video_Starttime',
                     f'{start + head / 1000.0 * rate:.3f}')
        return ','.join(tokens), 'slow/accelerate video converted to manual speed'

    if head == 0:
        return None, None                   # only the end was trimmed: fine

    if treat in ('Manual', 'Manual and Loop'):
        new_start = start + head / 1000.0 * speed
    else:                                   # Normal, Normal No Blue, Loop
        new_start = start + head / 1000.0
    if 'Loop' in treat:
        length = _video_length(fn)
        if length:
            new_start %= length
    new_start = max(0.0, new_start)
    _put_setting(tokens, 'E_TEXTCTRL_Video_Starttime', f'{new_start:.3f}')
    return ','.join(tokens), None


def extract_clip_effects(parser, start_ms, end_ms):
    """
    Extract effects from a parsed XSQ within a time range.
    Times are shifted so start_ms becomes 0.
    Effects spanning boundaries are clipped; Video effects that are
    trimmed get their video start time moved so frames stay in sync.
    """
    time_offset = start_ms

    used_palettes = set()
    used_effects_refs = set()
    effect_db = list(parser.effect_db)      # may grow with re-timed videos
    added_db = {}                           # settings text -> new index
    video_stats = {'retimed': 0, 'notes': set()}

    def process_effect(eff_dict):
        """Check if effect is in range; clip and shift if needed."""
        st = int(eff_dict.get('startTime', '0'))
        et = int(eff_dict.get('endTime', '0'))

        # Skip if completely outside range
        if et <= start_ms or st >= end_ms:
            return None

        # Clip to range and shift
        new_st = max(st, start_ms) - time_offset
        new_et = min(et, end_ms) - time_offset

        new_eff = dict(eff_dict)
        new_eff['startTime'] = str(new_st)
        new_eff['endTime'] = str(new_et)

        # Re-time trimmed Video effects (new EffectDB entry, original untouched)
        if eff_dict.get('name') == 'Video' and 'ref' in eff_dict \
                and (st < start_ms or et > end_ms):
            try:
                ref = int(eff_dict['ref'])
                text = effect_db[ref]
            except (ValueError, IndexError):
                text = None
            if text is not None:
                new_text, note = adjust_video_settings(
                    text, st, et, max(st, start_ms), min(et, end_ms))
                if note:
                    video_stats['notes'].add(note)
                if new_text and new_text != text:
                    if new_text not in added_db:
                        added_db[new_text] = len(effect_db)
                        effect_db.append(new_text)
                    new_eff['ref'] = str(added_db[new_text])
                    video_stats['retimed'] += 1

        # Track used indices
        if 'ref' in new_eff:
            try:
                used_effects_refs.add(int(new_eff['ref']))
            except ValueError:
                pass
        if 'palette' in new_eff:
            try:
                used_palettes.add(int(new_eff['palette']))
            except ValueError:
                pass

        return new_eff

    # Process all elements
    clipped_elements = []
    for elem in parser.element_effects:
        new_elem = {
            'type': elem['type'],
            'name': elem['name'],
            'layers': [],
            'submodels': [],
            'strands': [],
        }

        for layer in elem['layers']:
            new_layer = {'effects': []}
            for eff in layer['effects']:
                new_eff = process_effect(eff)
                if new_eff:
                    new_layer['effects'].append(new_eff)
            new_elem['layers'].append(new_layer)

        for sub in elem['submodels']:
            new_sub = {'name': sub['name'], 'effects': []}
            for eff in sub['effects']:
                new_eff = process_effect(eff)
                if new_eff:
                    new_sub['effects'].append(new_eff)
            new_elem['submodels'].append(new_sub)

        for strand in elem['strands']:
            new_strand = {'index': strand['index'], 'layers': []}
            for layer in strand['layers']:
                new_layer = {'effects': []}
                for eff in layer['effects']:
                    new_eff = process_effect(eff)
                    if new_eff:
                        new_layer['effects'].append(new_eff)
                new_strand['layers'].append(new_layer)
            new_elem['strands'].append(new_strand)

        clipped_elements.append(new_elem)

    return {
        'elements': clipped_elements,
        'used_palettes': used_palettes,
        'used_effects_refs': used_effects_refs,
        'duration_ms': end_ms - start_ms,
        'palettes': list(parser.color_palettes),
        'effect_db': effect_db,
        'display_elements': parser.display_elements,
        'videos_retimed': video_stats['retimed'],
        'video_notes': sorted(video_stats['notes']),
    }


def _drop_setting(tokens, key):
    pre = key + '='
    return [t for t in tokens if not t.startswith(pre)]


_OUT_TRANSITION_EXTRAS = ('T_CHOICE_Out_Transition_Type', 'T_SLIDER_Out_Transition_Adjust',
                          'T_VALUECURVE_Out_Transition_Adjust',
                          'T_CHECKBOX_Out_Transition_Reverse')


def apply_end_fade(merged_element_effects, effect_db, total_ms, fade_ms):
    """
    Fade the lights to black over the last fade_ms of the sequence using
    xLights' per-effect Fade Out transition (T_TEXTCTRL_Fadeout, seconds;
    'Fade' is the default out-transition type, so any other type such as
    Wipe is removed).

    * effects reaching the end fade over the whole window (or their whole
      length if shorter), hitting black exactly at the end
    * effects ending inside the window fade from the window start, so they
      reach black when they end instead of cutting off at full brightness
    * an existing longer fade-out is kept; fade-in is shortened if fade-in
      + fade-out would exceed the effect's length

    Model effects only (timing tracks are untouched). Returns the count of
    effects changed. Changed settings get new EffectDB entries.
    """
    if fade_ms <= 0:
        return 0
    w0 = total_ms - fade_ms
    text_idx = {t: i for i, t in enumerate(effect_db)}
    memo = {}
    count = 0

    def fade(eff):
        nonlocal count
        try:
            s, e, ref = int(eff['startTime']), int(eff['endTime']), int(eff['ref'])
        except (KeyError, ValueError):
            return
        if e <= w0 or e <= s or not (0 <= ref < len(effect_db)):
            return
        dur = (e - s) / 1000.0
        reaches_end = e >= total_ms - 50
        target = min(fade_ms if reaches_end else e - w0, e - s) / 1000.0
        key = (ref, round(target, 2), round(dur, 2))
        if key not in memo:
            tokens = [t for t in effect_db[ref].split(',') if t] if effect_db[ref] else []
            had_other_type = _setting(tokens, 'T_CHOICE_Out_Transition_Type') not in (None, 'Fade')
            try:
                cur = float(_setting(tokens, 'T_TEXTCTRL_Fadeout') or 0)
            except ValueError:
                cur = 0.0
            out = min(dur, max(target, 0 if had_other_type else cur))
            for k in _OUT_TRANSITION_EXTRAS:
                tokens = _drop_setting(tokens, k)
            _put_setting(tokens, 'T_TEXTCTRL_Fadeout', f'{out:.2f}')
            try:
                fin = float(_setting(tokens, 'T_TEXTCTRL_Fadein') or 0)
            except ValueError:
                fin = 0.0
            if fin + out > dur:
                _put_setting(tokens, 'T_TEXTCTRL_Fadein', f'{max(0.0, dur - out):.2f}')
            text = ','.join(tokens)
            if text not in text_idx:
                text_idx[text] = len(effect_db)
                effect_db.append(text)
            memo[key] = text_idx[text]
        eff['ref'] = str(memo[key])
        count += 1

    for me in merged_element_effects.values():
        if me.get('type') == 'timing':
            continue
        for layer in me['layers']:
            for eff in layer['effects']:
                fade(eff)
        for sub in me['submodels'].values():
            for eff in sub['effects']:
                fade(eff)
        for strand in me['strands'].values():
            for layer in strand['layers']:
                for eff in layer['effects']:
                    fade(eff)
    return count


def merge_clips_to_xsq(clips_data, output_path, media_file_path,
                        total_duration_ms, crossfade_ms=0, fade_out_ms=0):
    """
    Merge multiple clip extractions into a single XSQ file.

    Handles:
    - Palette and EffectDB deduplication with index remapping
    - Time offset accumulation (with crossfade overlap)
    - DisplayElement union
    - Effect ID renumbering
    """
    # Build merged palettes and effect_db with remapping
    merged_palettes = []
    merged_effect_db = []
    palette_map = {}   # (clip_idx, old_idx) -> new_idx
    effect_map = {}    # (clip_idx, old_idx) -> new_idx

    # Dedup caches
    palette_to_idx = {}
    effect_to_idx = {}

    for ci, clip in enumerate(clips_data):
        for old_idx in clip['used_palettes']:
            if old_idx < len(clip['palettes']):
                pal_text = clip['palettes'][old_idx]
                if pal_text in palette_to_idx:
                    palette_map[(ci, old_idx)] = palette_to_idx[pal_text]
                else:
                    new_idx = len(merged_palettes)
                    merged_palettes.append(pal_text)
                    palette_to_idx[pal_text] = new_idx
                    palette_map[(ci, old_idx)] = new_idx

        for old_idx in clip['used_effects_refs']:
            if old_idx < len(clip['effect_db']):
                eff_text = clip['effect_db'][old_idx]
                if eff_text in effect_to_idx:
                    effect_map[(ci, old_idx)] = effect_to_idx[eff_text]
                else:
                    new_idx = len(merged_effect_db)
                    merged_effect_db.append(eff_text)
                    effect_to_idx[eff_text] = new_idx
                    effect_map[(ci, old_idx)] = new_idx

    # Merge display elements (union by name, preserving order)
    all_display_elements = {}
    display_order = []
    for clip in clips_data:
        for de in clip['display_elements']:
            name = de.get('name', '')
            if name not in all_display_elements:
                all_display_elements[name] = de
                display_order.append(name)

    # Merge element effects with time offset
    merged_element_effects = {}
    cumulative_offset = 0

    for ci, clip in enumerate(clips_data):
        for elem in clip['elements']:
            name = elem['name']
            etype = elem['type']

            if name not in merged_element_effects:
                merged_element_effects[name] = {
                    'type': etype,
                    'name': name,
                    'layers': [],
                    'submodels': {},
                    'strands': {},
                }

            me = merged_element_effects[name]

            # Ensure enough layers
            while len(me['layers']) < len(elem['layers']):
                me['layers'].append({'effects': []})

            def remap_effect(eff, clip_idx, time_off):
                new_eff = dict(eff)
                new_eff['startTime'] = str(int(eff['startTime']) + time_off)
                new_eff['endTime'] = str(int(eff['endTime']) + time_off)
                if 'ref' in new_eff:
                    try:
                        old_ref = int(new_eff['ref'])
                        new_eff['ref'] = str(effect_map.get(
                            (clip_idx, old_ref), old_ref))
                    except ValueError:
                        pass
                if 'palette' in new_eff:
                    try:
                        old_pal = int(new_eff['palette'])
                        new_eff['palette'] = str(palette_map.get(
                            (clip_idx, old_pal), old_pal))
                    except ValueError:
                        pass
                # Remove 'selected' attribute if present
                new_eff.pop('selected', None)
                return new_eff

            for li, layer in enumerate(elem['layers']):
                for eff in layer['effects']:
                    me['layers'][li]['effects'].append(
                        remap_effect(eff, ci, cumulative_offset))

            for sub in elem['submodels']:
                sub_name = sub['name']
                if sub_name not in me['submodels']:
                    me['submodels'][sub_name] = {'effects': []}
                for eff in sub['effects']:
                    me['submodels'][sub_name]['effects'].append(
                        remap_effect(eff, ci, cumulative_offset))

            for strand in elem['strands']:
                s_idx = strand['index']
                if s_idx not in me['strands']:
                    me['strands'][s_idx] = {'layers': []}
                ms = me['strands'][s_idx]
                while len(ms['layers']) < len(strand['layers']):
                    ms['layers'].append({'effects': []})
                for li, layer in enumerate(strand['layers']):
                    for eff in layer['effects']:
                        ms['layers'][li]['effects'].append(
                            remap_effect(eff, ci, cumulative_offset))

        # Advance offset (accounting for crossfade overlap)
        cumulative_offset += clip['duration_ms']
        if crossfade_ms > 0 and ci < len(clips_data) - 1:
            cumulative_offset -= crossfade_ms

    # Fade the lights out over the last few seconds (xLights Fade Out transition)
    faded = apply_end_fade(merged_element_effects, merged_effect_db,
                           total_duration_ms, fade_out_ms)
    if faded:
        print(f"     faded out {faded} effects over the last {fade_out_ms / 1000:.1f}s")

    # Renumber effect IDs within each element/layer
    for me in merged_element_effects.values():
        for layer in me['layers']:
            _renumber_ids(layer['effects'])
        for sub in me['submodels'].values():
            _renumber_ids(sub['effects'])
        for strand in me['strands'].values():
            for layer in strand['layers']:
                _renumber_ids(layer['effects'])

    # Build the XSQ XML document
    root = ET.Element('xsequence')
    root.set('BaseChannel', '0')
    root.set('ChanCtrlBasic', '0')
    root.set('ChanCtrlColor', '0')
    root.set('FixedPointTiming', '1')
    root.set('ModelBlending', 'true')

    # Head
    head = ET.SubElement(root, 'head')
    ET.SubElement(head, 'version').text = '2024.01'
    ET.SubElement(head, 'author').text = ''
    ET.SubElement(head, 'author-email').text = ''
    ET.SubElement(head, 'author-website').text = ''
    ET.SubElement(head, 'song').text = 'Mashup'
    ET.SubElement(head, 'artist').text = ''
    ET.SubElement(head, 'album').text = ''
    ET.SubElement(head, 'MusicURL').text = ''
    ET.SubElement(head, 'comment').text = 'Generated mashup sequence'
    ET.SubElement(head, 'sequenceTiming').text = '25 ms'
    ET.SubElement(head, 'sequenceType').text = 'Media'
    ET.SubElement(head, 'mediaFile').text = media_file_path
    ET.SubElement(head, 'sequenceDuration').text = str(
        round(total_duration_ms / 1000.0, 3))
    ET.SubElement(head, 'imageDir')

    ET.SubElement(root, 'nextid').text = '1'
    ET.SubElement(root, 'Jukebox')

    # ColorPalettes
    cp_elem = ET.SubElement(root, 'ColorPalettes')
    for pal in merged_palettes:
        p = ET.SubElement(cp_elem, 'ColorPalette')
        p.text = pal

    # EffectDB
    edb = ET.SubElement(root, 'EffectDB')
    for eff in merged_effect_db:
        e = ET.SubElement(edb, 'Effect')
        e.text = eff

    # DataLayers
    dl = ET.SubElement(root, 'DataLayers')
    dli = ET.SubElement(dl, 'DataLayer')
    dli.set('lor_params', '0')
    dli.set('channel_offset', '0')
    dli.set('num_channels', '0')
    dli.set('num_frames', '0')
    dli.set('data', '<rendered: erase-mode>')
    dli.set('source', '<auto-generated>')
    dli.set('name', 'Nutcracker')

    # DisplayElements
    de_elem = ET.SubElement(root, 'DisplayElements')
    for name in display_order:
        de = all_display_elements[name]
        e = ET.SubElement(de_elem, 'Element')
        for k, v in de.items():
            e.set(k, v)

    # ElementEffects
    ee_elem = ET.SubElement(root, 'ElementEffects')
    for name in display_order:
        if name in merged_element_effects:
            me = merged_element_effects[name]
        else:
            de = all_display_elements[name]
            me = {
                'type': de.get('type', 'model'),
                'name': name,
                'layers': [{'effects': []}],
                'submodels': {},
                'strands': {},
            }

        elem_node = ET.SubElement(ee_elem, 'Element')
        elem_node.set('type', me['type'])
        elem_node.set('name', me['name'])

        for layer in me['layers']:
            layer_node = ET.SubElement(elem_node, 'EffectLayer')
            for eff in layer['effects']:
                eff_node = ET.SubElement(layer_node, 'Effect')
                for k, v in eff.items():
                    eff_node.set(k, str(v))

        for sub_name in sorted(me['submodels'].keys()):
            sub = me['submodels'][sub_name]
            sub_node = ET.SubElement(elem_node, 'SubModelEffectLayer')
            sub_node.set('name', sub_name)
            for eff in sub['effects']:
                eff_node = ET.SubElement(sub_node, 'Effect')
                for k, v in eff.items():
                    eff_node.set(k, str(v))

        for s_idx in sorted(me['strands'].keys()):
            strand = me['strands'][s_idx]
            strand_node = ET.SubElement(elem_node, 'Strand')
            strand_node.set('index', s_idx)
            for layer in strand['layers']:
                layer_node = ET.SubElement(strand_node, 'EffectLayer')
                for eff in layer['effects']:
                    eff_node = ET.SubElement(layer_node, 'Effect')
                    for k, v in eff.items():
                        eff_node.set(k, str(v))

    # lastView
    ET.SubElement(root, 'lastView').text = '0'

    # TimingTags
    tt_elem = ET.SubElement(root, 'TimingTags')
    for i in range(10):
        tag = ET.SubElement(tt_elem, 'Tag')
        tag.set('number', str(i))
        tag.set('position', '-1')

    # Write with XML declaration and indentation
    tree = ET.ElementTree(root)
    ET.indent(tree, space='  ')
    tree.write(output_path, encoding='UTF-8', xml_declaration=True)
    return faded


def _renumber_ids(effects_list):
    """Renumber the 'id' attribute on effects to be sequential."""
    for i, eff in enumerate(effects_list):
        if i == 0:
            eff.pop('id', None)  # First effect has no id
        else:
            eff['id'] = str(i)


# ─── Audio / Video Cutting ─────────────────────────────────────────────

# ─── Loudness normalisation (like mp3gain / ReplayGain, but EBU R128) ───

LOUDNESS_TARGET_LUFS = -16.0
_LOUDNESS_CACHE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                    'loudness_cache.json')
_loudness_cache = None


def measure_loudness(path):
    """
    Integrated loudness (LUFS) and true peak (dBTP) of a whole file.
    Measured once per file (cached by path + mtime + size).
    """
    global _loudness_cache
    if _loudness_cache is None:
        try:
            with open(_LOUDNESS_CACHE_PATH, 'r', encoding='utf-8') as f:
                _loudness_cache = json.load(f)
        except (OSError, ValueError):
            _loudness_cache = {}
    st = os.stat(path)
    key = f"{path}|{st.st_mtime}|{st.st_size}"
    if key in _loudness_cache:
        return _loudness_cache[key]

    cmd = [FFMPEG, '-hide_banner', '-nostats', '-i', path, '-vn',
           '-af', 'ebur128=peak=true:framelog=quiet', '-f', 'null', '-']
    r = subprocess.run(cmd, capture_output=True, text=True,
                       encoding='utf-8', errors='replace')
    summary = r.stderr[r.stderr.rfind('Summary:'):]
    mi = re.search(r'I:\s+(-?[\d.]+)\s+LUFS', summary)
    mp = re.search(r'Peak:\s+(-?[\d.]+)\s+dBFS', summary)
    if not mi:
        return None                      # silent / unreadable: leave as is
    result = {'lufs': float(mi.group(1)),
              'peak_db': float(mp.group(1)) if mp else 0.0}
    _loudness_cache[key] = result
    try:
        with open(_LOUDNESS_CACHE_PATH, 'w', encoding='utf-8') as f:
            json.dump(_loudness_cache, f)
    except OSError:
        pass
    return result


def loudness_gain_db(path, target=LOUDNESS_TARGET_LUFS):
    """(gain_db, needs_limiter) that brings this song to the target loudness."""
    m = measure_loudness(path)
    if not m:
        return 0.0, False
    gain = max(-20.0, min(12.0, target - m['lufs']))
    return gain, m['peak_db'] + gain > -1.0


def cut_audio(input_path, start_ms, end_ms, output_path, gain_db=0.0,
              limiter=False, fade_out_ms=0):
    """
    Cut a segment of audio using ffmpeg, optionally applying a gain.
    The gain is measured on the whole song (not the clip) so each song's
    own verse/chorus dynamics are kept. A true-peak limiter (with
    auto-levelling off and delay compensation on) catches peaks when
    boosting a quiet song.
    """
    start_s = start_ms / 1000.0
    duration_s = (end_ms - start_ms) / 1000.0

    filters = []
    if abs(gain_db) >= 0.1:
        filters.append(f'volume={gain_db:.2f}dB')
    if limiter:
        filters.append('alimiter=limit=0.891:level=0:latency=1')  # ≈ -1 dBFS
    if fade_out_ms > 0:
        # -ss is an output option here, so filters see the *source file's*
        # timestamps: the fade start must be given in source time.
        f = min(fade_out_ms, end_ms - start_ms) / 1000.0
        filters.append(f'afade=t=out:st={start_s + max(0.0, duration_s - f):.3f}:d={f:.3f}')

    cmd = [
        FFMPEG, '-y',
        '-i', input_path,
        '-ss', f'{start_s:.3f}',
        '-t', f'{duration_s:.3f}',
    ] + (['-af', ','.join(filters)] if filters else []) + [
        '-acodec', 'libmp3lame',
        '-q:a', '2',
        output_path
    ]

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg audio cut error: {result.stderr[-500:]}")
    return output_path


def concat_audio(audio_files, output_path, crossfade_ms=0):
    """Concatenate multiple audio files, optionally with crossfade."""
    if len(audio_files) == 0:
        raise ValueError("No audio files to concatenate")

    if len(audio_files) == 1:
        shutil.copy2(audio_files[0], output_path)
        return output_path

    if crossfade_ms <= 0:
        # Simple concat using ffmpeg concat demuxer
        list_file = output_path + '.txt'
        with open(list_file, 'w', encoding='utf-8') as f:
            for af in audio_files:
                safe_path = af.replace("'", "'\\''")
                f.write(f"file '{safe_path}'\n")

        cmd = [
            FFMPEG, '-y',
            '-f', 'concat',
            '-safe', '0',
            '-i', list_file,
            '-acodec', 'libmp3lame',
            '-q:a', '2',
            output_path
        ]

        result = subprocess.run(cmd, capture_output=True, text=True)
        try:
            os.remove(list_file)
        except OSError:
            pass
        if result.returncode != 0:
            raise RuntimeError(f"ffmpeg concat error: {result.stderr[-500:]}")
    else:
        # Crossfade concat using ffmpeg filter_complex
        cf_s = crossfade_ms / 1000.0
        n = len(audio_files)

        inputs = []
        for af in audio_files:
            inputs.extend(['-i', af])

        # Build filter chain: sequentially crossfade pairs
        filter_parts = []
        if n == 2:
            filter_parts.append(
                f'[0][1]acrossfade=d={cf_s}:c1=tri:c2=tri[out]')
            last_label = 'out'
        else:
            # First pair
            filter_parts.append(
                f'[0][1]acrossfade=d={cf_s}:c1=tri:c2=tri[cf1]')
            prev_label = 'cf1'
            for i in range(2, n):
                curr_label = f'cf{i}' if i < n - 1 else 'out'
                filter_parts.append(
                    f'[{prev_label}][{i}]acrossfade=d={cf_s}:c1=tri:c2=tri[{curr_label}]')
                prev_label = curr_label
            last_label = 'out'

        filter_str = ';'.join(filter_parts)

        cmd = [FFMPEG, '-y'] + inputs + [
            '-filter_complex', filter_str,
            '-map', f'[{last_label}]',
            '-acodec', 'libmp3lame',
            '-q:a', '2',
            output_path
        ]

        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(f"ffmpeg crossfade error: {result.stderr[-500:]}")

    return output_path


def cut_video(input_path, start_ms, end_ms, output_path):
    """Cut a segment of video using ffmpeg (for video effects)."""
    start_s = start_ms / 1000.0
    duration_s = (end_ms - start_ms) / 1000.0

    cmd = [
        FFMPEG, '-y',
        '-i', input_path,
        '-ss', f'{start_s:.3f}',
        '-t', f'{duration_s:.3f}',
        '-c', 'copy',
        output_path
    ]

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"ffmpeg video cut error: {result.stderr[-500:]}")
    return output_path


# ─── Main Build Orchestrator ───────────────────────────────────────────

def build_mashup(clips_config, output_dir, mashup_name='mashup',
                 normalize_audio=True, fade_out_ms=0):
    """
    Build a mashup from clip configurations.

    Args:
        clips_config: list of dicts with:
            - xsq_path: path to the .xsq file
            - start_ms: start time in milliseconds
            - end_ms: end time in milliseconds
        output_dir: directory to write output files
        mashup_name: name for the output files

    Returns:
        dict with output file paths and metadata.
    """
    os.makedirs(output_dir, exist_ok=True)

    crossfade_ms = int(clips_config[0].get('crossfade_ms', 0)) \
        if clips_config else 0

    progress = {'step': '', 'pct': 0}

    # Step 1: Parse all sequences and extract clips
    clips_data = []
    audio_segments = []

    for i, clip_cfg in enumerate(clips_config):
        pct = int((i / len(clips_config)) * 60)
        progress['step'] = f'Parsing clip {i+1}/{len(clips_config)}'
        progress['pct'] = pct
        print(f"[{pct}%] {progress['step']}: "
              f"{os.path.basename(clip_cfg['xsq_path'])}")

        parser = XSQParser(clip_cfg['xsq_path'])
        start_ms = int(clip_cfg['start_ms'])
        end_ms = int(clip_cfg['end_ms'])

        # Clamp to sequence duration
        end_ms = min(end_ms, parser.duration_ms)
        start_ms = max(0, start_ms)

        clip_data = extract_clip_effects(parser, start_ms, end_ms)
        clips_data.append(clip_data)

        # Ending fade lives inside the last clip, clear of its crossfade
        seg_fade = 0
        if fade_out_ms > 0 and i == len(clips_config) - 1:
            room = (end_ms - start_ms) - (crossfade_ms if len(clips_config) > 1 else 0) - 100
            seg_fade = int(max(0, min(fade_out_ms, room)))
            fade_out_ms = seg_fade            # lights use the same window

        # Cut audio segment — prefer the resolved media path (the one in the
        # XSQ header is often stale). Missing audio would silently desync the
        # mashup, so it's an error.
        media = clip_cfg.get('media_file') or parser.media_file
        if media and os.path.exists(media):
            seg_path = os.path.join(output_dir, f'_segment_{i:03d}.mp3')
            gain, limit = (loudness_gain_db(media) if normalize_audio
                           else (0.0, False))
            if normalize_audio:
                print(f"     loudness gain {gain:+.1f} dB"
                      f"{' (with limiter)' if limit else ''}")
            cut_audio(media, start_ms, end_ms, seg_path, gain, limit, seg_fade)
            audio_segments.append(seg_path)
            clip_data['gain_db'] = round(gain, 1)
        else:
            raise FileNotFoundError(
                f"Media file not found for {os.path.basename(clip_cfg['xsq_path'])}: {media}")

    # Step 2: Concatenate audio
    progress['step'] = 'Concatenating audio'
    progress['pct'] = 65
    print(f"[65%] {progress['step']}...")
    mashup_audio = os.path.join(output_dir, f'{mashup_name}.mp3')
    concat_audio(audio_segments, mashup_audio, crossfade_ms=crossfade_ms)

    # Step 3: Calculate total duration
    total_duration_ms = sum(c['duration_ms'] for c in clips_data)
    if crossfade_ms > 0 and len(clips_data) > 1:
        total_duration_ms -= crossfade_ms * (len(clips_data) - 1)

    # Step 4: Build merged XSQ
    progress['step'] = 'Building mashup sequence'
    progress['pct'] = 80
    print(f"[80%] {progress['step']}...")
    mashup_xsq = os.path.join(output_dir, f'{mashup_name}.xsq')
    faded = merge_clips_to_xsq(
        clips_data, mashup_xsq, mashup_audio,
        total_duration_ms, crossfade_ms, fade_out_ms)

    # Cleanup temp segments
    for seg in audio_segments:
        try:
            os.remove(seg)
        except OSError:
            pass

    progress['step'] = 'Complete'
    progress['pct'] = 100
    print(f"[100%] Mashup complete! Duration: "
          f"{total_duration_ms/1000:.1f}s")

    return {
        'xsq_path': mashup_xsq,
        'audio_path': mashup_audio,
        'duration_ms': total_duration_ms,
        'duration_s': round(total_duration_ms / 1000.0, 3),
        'num_clips': len(clips_data),
        'num_palettes': len(merged_palettes) if 'merged_palettes' in dir() else 0,
        'mashup_name': mashup_name,
        'videos_retimed': sum(c.get('videos_retimed', 0) for c in clips_data),
        'video_notes': sorted({n for c in clips_data for n in c.get('video_notes', [])}),
        'gains_db': [c.get('gain_db') for c in clips_data],
        'normalized': bool(normalize_audio),
        'fade_out_ms': fade_out_ms,
        'effects_faded': faded or 0,
    }
