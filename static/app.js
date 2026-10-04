// SPDX-License-Identifier: AGPL-3.0-or-later
// SPDX-FileCopyrightText: 2026 Paul Taylor

'use strict';

// What a browser will decode; everything else gets the copy-path action instead
// of a player that fails. A record with a playback proxy is playable whatever
// the original was (docs/index-format.md, section 6).
const PLAYABLE_CONTAINERS = new Set(['wav', 'mp3', 'm4a']);

const DURATION_STEPS = 1000;

const DATE_SOURCE_LABEL = {
  container_metadata: 'from the file metadata',
  filename: 'from the file name',
  file_mtime: 'from the file date — copied files can be wrong',
  user_override: 'corrected by hand',
  unknown: 'not known',
};

// Which index versions this page understands, stated by the server in
// /config.json so the check cannot drift from the server that serves the page.
let SUPPORTED_INDEX_VERSIONS = new Set();

const state = {
  config: null,
  manifest: null,
  assets: [],
  ratings: {},   // star ratings, keyed by asset id — the viewer's own data
  selected: null,
  filters: new Map(),   // axis -> Set(values)
  duration: { loPos: 0, hiPos: DURATION_STEPS },
  durationExtent: null,
  query: '',
  sort: 'date-desc',
  audio: null,
  player: null,   // the drawer's player widgets, updated by syncPlayer()
};

/* --------------------------------------------------------------- helpers */

const n = v => (typeof v === 'number' && isFinite(v) ? v : null);

function el(tag, props, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props || {})) {
    if (key === 'class') node.className = value;
    else if (key === 'text') node.textContent = value;
    else if (key === 'html') node.innerHTML = value;
    else if (key.startsWith('on')) node.addEventListener(key.slice(2), value);
    else if (value !== null && value !== undefined) node.setAttribute(key, value);
  }
  for (const child of children.flat()) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

// Everything from the index is inserted as text, never as markup: a transcript
// or a label could contain anything, and this is the whole of the defence.
function duration(seconds) {
  const s = n(seconds);
  if (s === null) return '—';
  const total = Math.round(s);
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const sec = total % 60;
  return h ? `${h}h ${m}m` : `${m}:${String(sec).padStart(2, '0')}`;
}

function when(iso) {
  if (!iso) return 'undated';
  const d = new Date(iso);
  if (isNaN(d)) return iso;
  return d.toLocaleDateString(undefined, { day: 'numeric', month: 'short', year: 'numeric' });
}

const year = iso => (iso ? String(iso).slice(0, 4) : 'undated');
const fileName = path => String(path || '').split('/').pop();
const enc = rel => String(rel || '').split('/').map(encodeURIComponent).join('/');
const container = a => (a.technical && a.technical.container) || '?';

const waveURL = a => (a.wave && a.wave.path) ? `/index/${enc(a.wave.path)}` : null;
const specURL = a => (a.spec && a.spec.path) ? `/index/${enc(a.spec.path)}` : null;

const playable = a => {
  if (a.playback && a.playback.path) return true;
  return PLAYABLE_CONTAINERS.has(String(container(a)).toLowerCase());
};

// Play the proxy when there is one, else the original bytes via /media.
const mediaURL = a => {
  if (a.playback && a.playback.path) return `/index/${enc(a.playback.path)}`;
  if (playable(a)) return `/media/${enc(a.source.path)}`;
  return null;
};

// The absolute path for the clipboard, resolved by the server half.
const mediaPath = a => {
  const root = (state.config && state.config.media_root) ||
               (state.manifest && state.manifest.media_root) || '';
  const rel = a.source && a.source.path;
  return rel ? `${root}/${rel}` : null;
};

const labelText = a => (a.labels || []).map(l => l.text);
const eventText = a => (a.events || []).map(e => e.text);
const transcriptText = a => (a.transcription && a.transcription.text) || '';

function searchHaystack(a) {
  return [
    a.id,
    a.source && a.source.path,
    container(a),
    (a.technical || {}).codec || '',
    a.captured && a.captured.device ? Object.values(a.captured.device).join(' ') : '',
    ...labelText(a),
    ...eventText(a),
    transcriptText(a),
  ].join(' ').toLowerCase();
}

/* ----------------------------------------------------------------- facets */

// The three browse axes, per requirement 13: when, what, and format. "How long"
// is a range over a measurement, rendered beside them but not an axis.
const AXES = [
  { key: 'when', title: 'When',
    values: a => {
      const v = [];
      if (!a.captured || !a.captured.at) v.push(['date unknown', 'date-unknown']);
      else v.push([year(a.captured.at), `y:${year(a.captured.at)}`]);
      if (a.captured && (a.captured.at_source === 'file_mtime' || a.captured.at_source === 'filename')) {
        v.push(['date approximate', 'date-approximate']);
      }
      return v;
    },
    label: v => v.startsWith('y:') ? v.slice(2) : v },
  { key: 'what', title: 'Sounds',
    values: a => (a.labels || []).map(l => [l.text, l.text, !!l.weak]),
    label: v => v },
  { key: 'format', title: 'Format',
    values: a => [[container(a), container(a)]],
    label: v => v },
  { key: 'rating', title: 'Rating',
    values: a => {
      const stars = state.ratings[a.id] || 0;
      return stars ? [[`${stars} star${stars > 1 ? 's' : ''}`, `r:${stars}`]] : [['unrated', 'r:0']];
    },
    label: v => v === 'r:0' ? 'unrated' : `${v.slice(2)} star${v.slice(2) === '1' ? '' : 's'}` },
];

const axisFor = key => AXES.find(axis => axis.key === key);
const valuesOf = (axis, a) => axis.values(a)
  .map(entry => ({ value: entry[1], label: entry[0], weak: entry[2] }));

function matches(a, ignoreAxis) {
  if (state.query && !searchHaystack(a).includes(state.query)) return false;
  if (ignoreAxis !== 'duration' && !durationMatches(a)) return false;
  for (const [key, chosen] of state.filters) {
    if (key === ignoreAxis || chosen.size === 0) continue;
    const values = valuesOf(axisFor(key), a).map(entry => entry.value);
    if (!values.some(value => chosen.has(value))) return false;
  }
  return true;
}

/* --------------------------------------------------------------- duration */

function measureDurationExtent() {
  let shortest = null, longest = null;
  for (const asset of state.assets) {
    const d = n(asset.technical && asset.technical.duration_s);
    if (d === null) continue;
    shortest = shortest === null ? d : Math.min(shortest, d);
    longest = longest === null ? d : Math.max(longest, d);
  }
  state.durationExtent = shortest === null ? null : {
    lo: shortest >= 1 ? Math.floor(shortest) : shortest,
    hi: Math.max(2, Math.ceil(longest)),
  };
}

function positionSeconds(position) {
  const { lo, hi } = state.durationExtent;
  if (position <= 0) return 0;
  return lo * Math.pow(hi / lo, (position - 1) / (DURATION_STEPS - 1));
}

function durationRange() {
  if (!state.durationExtent) return { lo: 0, hi: 0, narrowed: false };
  const { loPos, hiPos } = state.duration;
  const lo = Math.floor(positionSeconds(loPos));
  const hi = Math.ceil(positionSeconds(hiPos));
  return { lo, hi, narrowed: lo > 0 || hiPos < DURATION_STEPS };
}

function durationMatches(asset, range = durationRange()) {
  if (!range.narrowed) return true;
  const d = n(asset.technical && asset.technical.duration_s);
  return d !== null && d >= range.lo && d <= range.hi;
}

function resetDuration() {
  state.duration.loPos = 0;
  state.duration.hiPos = DURATION_STEPS;
}

function renderDuration() {
  const facet = el('div', { class: 'facet' }, el('h2', { text: 'How long' }));
  if (!state.durationExtent) {
    facet.append(el('div', { class: 'note', text: 'no durations were measured' }));
    return facet;
  }

  const thumb = (id, value, label) => el('input', {
    type: 'range', id, min: '0', max: String(DURATION_STEPS), step: '1',
    value: String(value), 'aria-label': label,
  });
  const low = thumb('duration-lo', state.duration.loPos, 'shortest duration');
  const high = thumb('duration-hi', state.duration.hiPos, 'longest duration');

  const fill = el('div', { class: 'range-fill' });
  const lowText = el('span');
  const countText = el('span', { class: 'n' });
  const highText = el('span');

  const paint = () => {
    const range = durationRange();
    const { loPos, hiPos } = state.duration;
    fill.style.left = `${(loPos / DURATION_STEPS) * 100}%`;
    fill.style.width = `${((hiPos - loPos) / DURATION_STEPS) * 100}%`;
    lowText.textContent = duration(range.lo);
    highText.textContent = duration(range.hi);
    countText.textContent = String(state.assets
      .filter(a => matches(a, 'duration') && durationMatches(a, range)).length);
    low.setAttribute('aria-valuetext', duration(range.lo));
    high.setAttribute('aria-valuetext', duration(range.hi));
  };

  const moved = (edge, input) => {
    const key = edge === 'lo' ? 'loPos' : 'hiPos';
    if (edge === 'lo') state.duration.loPos = Math.min(Number(input.value), state.duration.hiPos);
    else state.duration.hiPos = Math.max(Number(input.value), state.duration.loPos);
    input.value = String(state.duration[key]);
  };

  const commit = (edge, input) => {
    const keepFocus = document.activeElement === input;
    moved(edge, input);
    render();
    if (keepFocus) document.getElementById(input.id) && document.getElementById(input.id).focus({ preventScroll: true });
  };

  low.addEventListener('input', () => { moved('lo', low); paint(); });
  high.addEventListener('input', () => { moved('hi', high); paint(); });
  low.addEventListener('change', () => commit('lo', low));
  high.addEventListener('change', () => commit('hi', high));

  paint();
  facet.append(el('div', { class: 'range' },
    el('div', { class: 'range-track' }), fill, low, high),
    el('div', { class: 'range-readout' }, lowText, countText, highText));
  return facet;
}

/* ------------------------------------------------------------------- sort */

const SORTS = {
  'date-desc': (a, b) => (b.captured?.at || '').localeCompare(a.captured?.at || ''),
  'date-asc': (a, b) => (a.captured?.at || 'zzz').localeCompare(b.captured?.at || 'zzz'),
  'duration': (a, b) => (n(b.technical?.duration_s) || 0) - (n(a.technical?.duration_s) || 0),
  'name': (a, b) => fileName(a.source?.path).localeCompare(fileName(b.source?.path)),
};

const visible = () => state.assets.filter(a => matches(a)).sort(SORTS[state.sort]);

/* ----------------------------------------------------------------- render */

function render() {
  const items = visible();
  const total = state.assets.length;
  document.getElementById('count').textContent =
    items.length === total ? `${total} assets` : `${items.length} of ${total} assets`;

  const rail = document.getElementById('rail');
  const sections = [];
  for (const axis of AXES) {
    sections.push(renderFacet(axis));
    if (axis.key === 'when') sections.push(renderDuration());
  }
  rail.replaceChildren(...sections, renderClear());

  const grid = document.getElementById('grid');
  if (!items.length) {
    grid.replaceChildren(el('div', { class: 'empty-library', text: 'nothing matches' }));
    return;
  }
  grid.replaceChildren(...items.map(card));
}

function renderFacet(axis) {
  const counts = new Map();
  const weak = new Set();
  for (const asset of state.assets) {
    if (!matches(asset, axis.key)) continue;
    for (const entry of valuesOf(axis, asset)) {
      counts.set(entry.value, (counts.get(entry.value) || 0) + 1);
      if (entry.weak) weak.add(entry.value);
    }
  }
  const chosen = state.filters.get(axis.key) || new Set();
  const values = [...counts.entries()].sort((x, y) => {
    if (axis.key === 'when') return String(y[0]).localeCompare(String(x[0]));
    if (axis.key === 'rating') {
      const rank = v => (v === 'r:0' ? -1 : parseInt(v.slice(2), 10));
      return rank(y[0]) - rank(x[0]) || y[1] - x[1];
    }
    return y[1] - x[1] || String(x[0]).localeCompare(String(y[0]));
  });

  const chips = values.map(([value, count]) => {
    const pressed = chosen.has(value);
    return el('button', {
      class: `chip${weak.has(value) ? ' weak' : ''}${!pressed && count === 0 ? ' empty' : ''}`,
      'aria-pressed': String(pressed),
      title: weak.has(value) ? 'weak label — evidence is a guess, not a fact' : null,
      onclick: () => toggle(axis.key, value),
    }, axis.label(value), el('span', { class: 'n', text: String(count) }));
  });

  const node = el('div', { class: 'facet' }, el('h2', { text: axis.title }));
  node.append(el('div', {}, chips.length ? chips : el('div', { class: 'note', text: 'nothing here' })));
  return node;
}

function renderClear() {
  const active = state.filters.size > 0 || state.query || durationRange().narrowed;
  return el('div', {}, el('button', {
    class: 'clear', text: 'clear filters', disabled: active ? null : 'disabled',
    onclick: () => {
      state.filters.clear();
      state.query = '';
      resetDuration();
      document.getElementById('q').value = '';
      document.getElementById('q-clear').hidden = true;
      render();
    },
  }));
}

function card(a) {
  const w = waveURL(a);
  const thumb = el('div', { class: 'thumb' },
    w ? el('img', { src: w, alt: '', loading: 'lazy' })
      : el('div', { class: 'none', text: 'no waveform' }),
    el('span', { class: 'duration', text: duration(a.technical && a.technical.duration_s) }));

  const meta = el('div', { class: 'when' },
    el('span', { text: when(a.captured && a.captured.at) }),
    a.captured && (a.captured.at_source === 'file_mtime' || a.captured.at_source === 'filename')
      ? el('span', { class: 'approx', text: 'approx' }) : null);

  const name = el('div', { class: 'name', text: fileName(a.source.path) });
  const tags = el('div', { class: 'tags' },
    (a.labels || []).slice(0, 4).map(l => el('span', { class: 'tag' + (l.weak ? ' weak' : ''), text: l.text })));

  return el('div', {
    class: 'card',
    'data-selected': String(state.selected === a),
    onclick: () => openDrawer(a),
  }, thumb, el('div', { class: 'body' }, meta, name, starStrip(a), tags));
}

/* ----------------------------------------------------------------- drawer */

function ensureAudio() {
  if (!state.audio) {
    state.audio = new Audio();
    document.body.append(state.audio);
    state.audio.addEventListener('timeupdate', syncPlayer);
    state.audio.addEventListener('durationchange', syncPlayer);
    state.audio.addEventListener('play', syncPlayer);
    state.audio.addEventListener('pause', syncPlayer);
    state.audio.addEventListener('ended', syncPlayer);
  }
  return state.audio;
}

// One place that paints the player: the playhead over the spectrogram, the
// seek bar fill and thumb, the elapsed time and the play/pause glyph all follow
// the audio element, so a seek from the transcript, an event or the bar itself
// lands in the same spot as one made anywhere else.
function syncPlayer() {
  const p = state.player;
  if (!p || !state.audio) return;
  const audio = state.audio;
  const dur = (audio.duration && isFinite(audio.duration) && audio.duration > 0)
    ? audio.duration : (p.duration || 0);
  const t = Math.min(audio.currentTime || 0, dur || 0);
  const frac = dur > 0 ? t / dur : 0;
  if (p.playhead) {
    p.playhead.style.left = `${frac * 100}%`;
    p.playhead.classList.add('visible');
  }
  if (p.fill) p.fill.style.width = `${frac * 100}%`;
  if (p.thumb) p.thumb.style.left = `${frac * 100}%`;
  if (p.current) p.current.textContent = duration(t);
  if (p.play) p.play.textContent = audio.paused ? '▶' : '❚❚';
  if (p.total && audio.duration && isFinite(audio.duration) && audio.duration > 0) {
    p.total.textContent = duration(audio.duration);
  }
}

function seekAudio(a, seconds) {
  const url = mediaURL(a);
  if (!url) return;
  const audio = ensureAudio();
  if (audio.getAttribute('src') !== url) audio.src = url;
  audio.currentTime = Math.max(0, seconds);
  audio.play().catch(() => {});
  syncPlayer();
}

// A video-style seek bar: click or drag to move playback, with a fill and a
// thumb that syncPlayer() keeps on the elapsed time.
function makeSeek() {
  const track = el('div', { class: 'seek' });
  const rail = el('div', { class: 'seek-track' });
  const fill = el('div', { class: 'seek-fill' });
  const thumb = el('div', { class: 'seek-thumb' });
  track.append(rail, fill, thumb);

  const seekTo = (event) => {
    if (!state.audio) return;
    const audio = state.audio;
    const dur = (audio.duration && isFinite(audio.duration) && audio.duration > 0)
      ? audio.duration : (state.player ? state.player.duration : 0) || 0;
    if (!dur) return;
    const rect = track.getBoundingClientRect();
    const frac = Math.min(1, Math.max(0, (event.clientX - rect.left) / rect.width));
    audio.currentTime = frac * dur;
    syncPlayer();
  };

  let dragging = false;
  track.addEventListener('pointerdown', event => {
    dragging = true;
    if (track.setPointerCapture) track.setPointerCapture(event.pointerId);
    seekTo(event);
  });
  track.addEventListener('pointermove', event => { if (dragging) seekTo(event); });
  const stop = event => {
    if (!dragging) return;
    dragging = false;
    if (track.releasePointerCapture) track.releasePointerCapture(event.pointerId);
  };
  track.addEventListener('pointerup', stop);
  track.addEventListener('pointercancel', stop);
  return { track, fill, thumb };
}

function copyText(text, label) {
  const done = () => toast(label || 'copied');
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text).then(done, () => toast('copy failed'));
  } else {
    const area = document.createElement('textarea');
    area.value = text;
    document.body.append(area);
    area.select();
    try { document.execCommand('copy'); done(); } catch { toast('copy failed'); }
    area.remove();
  }
}

/* ----------------------------------------------------------------- rating */

// Five stars, click to set, click the same star again to clear. Drawn with ★ and
// ☆ rather than filled glyphs from a font, so the state is visible without colour
// and a screen reader still hears the number.
function starStrip(a, big = false) {
  const current = state.ratings[a.id] || 0;
  return el('span', { class: `stars${big ? ' big' : ''}` },
    Array.from({ length: 5 }, (_, i) => {
      const n = i + 1;
      const on = n <= current;
      return el('button', {
        class: `star${on ? ' on' : ''}`,
        text: on ? '★' : '☆',
        title: current === n ? 'clear the rating' : `${n} star${n > 1 ? 's' : ''}`,
        'aria-label': `${n} star${n > 1 ? 's' : ''}`,
        'aria-pressed': String(on),
        onclick: event => {
          event.stopPropagation();
          setRating(a.id, current === n ? 0 : n);
        },
      });
    }));
}

async function setRating(id, stars) {
  try {
    const response = await fetch('/rating', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ id, stars }),
    });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const saved = await response.json();
    if (saved.stars === 0) delete state.ratings[id];
    else state.ratings[id] = saved.stars;
    render();
    // The drawer is not rebuilt by render(), so its own stars are refreshed in
    // place — a full re-open would stop a recording that is playing.
    if (state.selected === id) {
      const box = document.querySelector('#drawer .rating-box');
      if (box) {
        const asset = state.assets.find(x => x.id === id);
        if (asset) box.replaceChildren(starStrip(asset, true));
      }
    }
  } catch (error) {
    toast('could not save the rating');
  }
}

function openDrawer(a) {
  state.selected = a;
  const drawer = document.getElementById('drawer');
  const backdrop = document.getElementById('backdrop');

  const wave = waveURL(a);
  const spec = specURL(a);
  const url = mediaURL(a);
  const path = mediaPath(a);

  const playhead = el('div', { class: 'playhead' });
  const stage = el('div', { class: 'stage' });
  const waveImg = wave ? el('img', { src: wave, alt: 'waveform' }) : null;
  const specImg = spec ? el('img', { src: spec, alt: 'spectrogram' }) : null;
  if (waveImg) stage.append(waveImg);
  if (specImg) stage.append(specImg);
  if (waveImg && specImg) {
    // The waveform is the default view; the spectrogram is an opt-in. Choosing
    // it lays the waveform over the spectrogram with a screen blend, so the
    // loudness envelope keeps tracing the pitch map.
    specImg.hidden = true;
    const toggle = el('button', {
      class: 'view-toggle', type: 'button', text: 'spectrogram',
      'aria-pressed': 'false',
      onclick: () => {
        const toSpec = specImg.hidden;
        specImg.hidden = !toSpec;
        waveImg.classList.toggle('wave-overlay', toSpec);
        waveImg.setAttribute('aria-hidden', toSpec ? 'true' : 'false');
        toggle.textContent = toSpec ? 'waveform' : 'spectrogram';
        toggle.setAttribute('aria-pressed', String(toSpec));
      },
    });
    stage.append(toggle);
  }
  stage.append(playhead);

  let player;
  if (url) {
    const audio = ensureAudio();
    if (audio.getAttribute('src') !== url) audio.src = url;
    const play = el('button', {
      class: 'play', title: 'Play / pause', 'aria-label': 'Play or pause',
      onclick: () => {
        if (audio.paused) audio.play().catch(() => {});
        else audio.pause();
        syncPlayer();
      },
    }, audio.paused ? '▶' : '❚❚');
    const current = el('span', { class: 'time', text: duration(audio.currentTime || 0) });
    const total = el('span', { class: 'time',
      text: duration((a.technical && a.technical.duration_s) || audio.duration) });
    const seek = makeSeek();
    player = el('div', { class: 'player' }, play, current, seek.track, total);
    state.player = {
      playhead, fill: seek.fill, thumb: seek.thumb, current, play, total,
      duration: n(a.technical && a.technical.duration_s) || 0,
    };
    syncPlayer();
  } else {
    player = el('div', { class: 'actions' },
      el('button', { class: 'primary', text: 'no player — copy the path' }));
    state.player = null;
  }

  const facts = el('div', { class: 'facts' },
    fact('duration', duration(a.technical && a.technical.duration_s)),
    fact('format', container(a)),
    fact('codec', (a.technical || {}).codec || '—'),
    fact('sample rate', a.technical && a.technical.sample_rate ? `${a.technical.sample_rate} Hz` : '—'),
    fact('channels', a.technical && a.technical.channels != null ? String(a.technical.channels) : '—'),
    fact('bit rate', a.technical && a.technical.bit_rate ? `${Math.round(a.technical.bit_rate / 1000)} kbps` : '—'),
  );

  const dateNote = a.captured && DATE_SOURCE_LABEL[a.captured.at_source]
    ? ` (${DATE_SOURCE_LABEL[a.captured.at_source]})` : '';

  const body = [
    stage,
    player,
    facts,
    el('div', { class: 'section' }, el('h3', { text: 'Rating' }),
      el('div', { class: 'rating-box' }, starStrip(a, true))),
    el('div', { class: 'when' },
      el('span', { text: `${when(a.captured && a.captured.at)}${dateNote}` })),
  ];

  if ((a.labels || []).length) {
    body.push(el('div', { class: 'section' }, el('h3', { text: 'Sounds' }),
      el('div', {}, (a.labels || []).map(l =>
        el('button', { class: 'chip' + (l.weak ? ' weak' : ''), onclick: () => { toggle('what', l.text); closeDrawer(); } },
          l.text, el('span', { class: 'n', text: l.score != null ? l.score.toFixed(2) : '' }))))));
  }

  if ((a.events || []).length) {
    body.push(el('div', { class: 'section' }, el('h3', { text: 'Sound events' }),
      el('div', {}, (a.events || []).map(e =>
        el('button', { class: 'event', onclick: () => seekAudio(a, e.at_s) },
          el('span', { class: 't', text: `${duration(e.at_s)}–${duration(e.at_e)}` }),
          el('span', { text: e.text }))))));
  }

  const segs = (a.transcription && a.transcription.segments) || [];
  if (segs.length) {
    body.push(el('div', { class: 'section' }, el('h3', { text: 'Transcript' }),
      el('div', {}, segs.map(s =>
        el('button', { class: 'seg', onclick: () => seekAudio(a, s.at_s) },
          el('span', { class: 't', text: duration(s.at_s) }),
          el('span', { text: s.text }))))));
  } else if (a.transcription && a.transcription.text) {
    body.push(el('div', { class: 'section' }, el('h3', { text: 'Transcript' }),
      el('p', { text: a.transcription.text })));
  }

  body.push(el('div', { class: 'path-line' },
    el('span', { text: path || '—' }),
    path ? el('button', { text: 'copy path', onclick: () => copyText(path, 'path copied') }) : null));

  drawer.replaceChildren(
    el('div', { class: 'drawer-head' },
      el('div', { class: 'who' },
        el('div', { text: fileName(a.source.path) }),
        el('div', { text: `${container(a)} · ${duration(a.technical && a.technical.duration_s)}` })),
      el('button', { class: 'close', 'aria-label': 'Close', onclick: closeDrawer }, '×')),
    el('div', { class: 'drawer-body' }, body));

  drawer.hidden = false;
  backdrop.hidden = false;
  render();
}

function fact(label, value) {
  return el('div', { class: 'fact' }, el('b', { text: label }), el('span', { text: value }));
}

function closeDrawer() {
  document.getElementById('drawer').hidden = true;
  document.getElementById('backdrop').hidden = true;
  if (state.audio) { state.audio.pause(); }
  state.player = null;
}

function toggle(axis, value) {
  let chosen = state.filters.get(axis);
  if (!chosen) { chosen = new Set(); state.filters.set(axis, chosen); }
  if (chosen.has(value)) chosen.delete(value); else chosen.add(value);
  if (chosen.size === 0) state.filters.delete(axis);
  render();
}

function toast(text) {
  const node = document.getElementById('toast');
  node.textContent = text;
  node.hidden = false;
  clearTimeout(node._timer);
  node._timer = setTimeout(() => { node.hidden = true; }, 1800);
}

/* ------------------------------------------------------------------- boot */

function boot() {
  const loading = document.getElementById('loading');
  loading.hidden = false;

  Promise.all([
    fetch('/config.json').then(r => r.json()),
    fetch('/index/manifest.json').then(r => r.json()),
    fetch('/ratings.json').then(r => r.json()).catch(() => ({ ratings: {} })),
  ])
    .then(([config, manifest, ratingsPayload]) => {
      state.config = config;
      state.manifest = manifest;
      state.ratings = (ratingsPayload && ratingsPayload.ratings) || {};
      SUPPORTED_INDEX_VERSIONS = new Set(config.supported_index_versions || []);
      if (manifest.index_version != null && !SUPPORTED_INDEX_VERSIONS.has(manifest.index_version)) {
        throw new Error(`index version ${manifest.index_version} is not supported by this interface`);
      }
      return fetch('/index/audio').then(r => r.json());
    })
    .then(({ ids }) => {
      const jobs = (ids || []).map(id => fetch(`/index/audio/${id}.json`).then(r => r.json()));
      return Promise.all(jobs);
    })
    .then(assets => {
      state.assets = assets.filter(a => a && a.id);
      measureDurationExtent();
      loading.hidden = true;
      render();
    })
    .catch(error => {
      loading.hidden = true;
      showBanner(String(error && error.message ? error.message : error));
    });
}

function showBanner(text) {
  const banner = document.getElementById('banner');
  document.getElementById('banner-text').textContent = text;
  banner.hidden = false;
}

function bind() {
  document.getElementById('q').addEventListener('input', event => {
    state.query = event.target.value.trim().toLowerCase();
    document.getElementById('q-clear').hidden = !state.query;
    render();
  });
  document.getElementById('q-clear').addEventListener('click', () => {
    state.query = '';
    document.getElementById('q').value = '';
    document.getElementById('q-clear').hidden = true;
    render();
  });
  document.getElementById('sort').addEventListener('change', event => {
    state.sort = event.target.value;
    render();
  });
  document.getElementById('rail-toggle').addEventListener('click', () => {
    document.getElementById('rail').classList.toggle('hidden');
  });
  document.getElementById('backdrop').addEventListener('click', closeDrawer);
  document.getElementById('banner-close').addEventListener('click', () => {
    document.getElementById('banner').hidden = true;
  });
  document.addEventListener('keydown', event => {
    if (event.key === 'Escape') closeDrawer();
    if (event.key === '/' && document.activeElement.tagName !== 'INPUT') {
      event.preventDefault();
      document.getElementById('q').focus();
    }
  });
}

bind();
boot();
