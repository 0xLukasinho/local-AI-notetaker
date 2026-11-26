// Reads Google Meet participant names, the active-speaker highlight, and
// live captions when the user has turned captions on. Posts them to the
// local meeting-notes bridge. Does not change the page, so other people in
// the call cannot see it. Captions being off just sends no caption lines.
//
// Meet obfuscates class names. Tiles use data-participant-id, data-self-name,
// a thick speaking ring on the tile itself, and a high data-audio-level.
// Captions prefer the region labeled Captions, then a few historical class
// names, then a name-plus-text structure. Any of those can disappear.

// Old speaking-class names matched layout on every tile after a Meet restyle,
// so every remote person was reported as talking. The ring and a clearly
// elevated audio level are the signals that still mean one person.
var PRESENCE_PORT = 8766;
var BRIDGE_PORT = 8765;
var SCRIPT_VERSION = "1.5.0";
var AUDIO_LEVEL_FLOOR = 8;
var BLOCKED_NAMES = [
  "mute",
  "unmute",
  "camera",
  "microphone",
  "present",
  "pin",
  "unpin",
  "more options",
  "more actions",
  "audio",
  "video",
  "background",
  "effects",
  "captions",
  "hand",
  "raise hand",
];

// Meet toasts sit inside the same tile as the name. They are sentences, not
// people. "Others might still see your full video." was stored as a participant.
var TOAST_PHRASES = [
  "might still",
  "your video",
  "your full video",
  "see your",
  "joined",
  "left the",
  "is presenting",
  "raised a hand",
  "turned on",
  "turned off",
];

var DENY_NAMES = {
  reframe: true,
  "font size": true,
  "others might still see your full video.": true,
  "others might still see your full video": true,
};

function plausibleName(text) {
  if (!text || text.length < 2 || text.length > 80) {
    return false;
  }
  if (/[.!?]/.test(text)) {
    return false;
  }
  if (!/\p{L}/u.test(text)) {
    return false;
  }
  const words = text.split(" ");
  if (words.length > 5) {
    return false;
  }
  const lower = text.toLowerCase();
  if (DENY_NAMES[lower]) {
    return false;
  }
  for (const word of BLOCKED_NAMES) {
    if (lower === word || lower.startsWith(word + " ")) {
      return false;
    }
  }
  for (const phrase of TOAST_PHRASES) {
    if (lower.indexOf(phrase) !== -1) {
      return false;
    }
  }
  return true;
}

function cleanLabel(text) {
  if (!text) {
    return "";
  }
  const line = text.replace(/\s+/g, " ").trim();
  const cut = line.split("(")[0].trim();
  return plausibleName(cut) ? cut : "";
}

function thickBorder(el) {
  if (!el || !el.ownerDocument) {
    return false;
  }
  let style;
  try {
    style = getComputedStyle(el);
  } catch (err) {
    return false;
  }
  if (!style) {
    return false;
  }
  const width = parseFloat(style.borderTopWidth) || 0;
  if (width < 3) {
    return false;
  }
  // Gray layout borders are not the speaking ring. Keep saturated colors.
  const color = style.borderTopColor || "";
  const match = color.match(/rgba?\((\d+),\s*(\d+),\s*(\d+)/);
  if (!match) {
    return true;
  }
  const red = Number(match[1]);
  const green = Number(match[2]);
  const blue = Number(match[3]);
  const max = Math.max(red, green, blue);
  const min = Math.min(red, green, blue);
  return max - min > 30 && max > 80;
}

function numericLevel(raw) {
  if (raw == null || raw === "") {
    return 0;
  }
  const value = parseFloat(String(raw).trim());
  if (!isFinite(value) || value < 0) {
    return 0;
  }
  return value;
}

function looksSpeaking(root) {
  // True only for the tile that Meet is actually highlighting. A colored
  // border or a tiny audio level on an inner node used to mark every tile.
  if (!root || !root.getAttribute) {
    return false;
  }
  const flag = String(root.getAttribute("data-is-speaking") || "").toLowerCase();
  if (flag === "true" || flag === "1") {
    return true;
  }
  if (thickBorder(root)) {
    return true;
  }
  if (numericLevel(root.getAttribute("data-audio-level")) >= AUDIO_LEVEL_FLOOR) {
    return true;
  }
  let bars = [];
  try {
    bars = root.querySelectorAll("[data-audio-level]");
  } catch (err) {
    bars = [];
  }
  let maxChild = 0;
  const limit = Math.min(bars.length, 4);
  for (let i = 0; i < limit; i++) {
    maxChild = Math.max(maxChild, numericLevel(bars[i].getAttribute("data-audio-level")));
  }
  return maxChild >= AUDIO_LEVEL_FLOOR;
}


// Current Meet (Nov 2026) puts no aria-label and no data-self-name on the
// [data-participant-id] tiles and no data-audio-level on them either. The
// name is a plain text label inside the tile, and the speaking indicator is
// an animated element whose classes and styles change many times a second
// while that person talks. Read the label text, and measure DOM activity per
// tile, instead of depending on class names Meet renames.
var ICON_FONT = /material|icons|symbols/i;
var ACTIVITY_WINDOW_MS = 1500;
var ACTIVITY_MIN = 4;
var tileActivity = new Map();

function iconText(el, text) {
  if (/_/.test(text)) {
    return true;
  }
  if (!/^[a-z]+$/.test(text)) {
    return false;
  }
  try {
    return ICON_FONT.test(getComputedStyle(el).fontFamily || "");
  } catch (err) {
    return false;
  }
}

function tileLabelTexts(tile) {
  // Leaf text inside the tile that is not a control, menu, or icon glyph.
  // Labels marked notranslate (Meet's name label) come first.
  const marked = [];
  const plain = [];
  let nodes = [];
  try {
    nodes = tile.querySelectorAll("div, span");
  } catch (err) {
    nodes = [];
  }
  const limit = Math.min(nodes.length, 400);
  for (let i = 0; i < limit; i++) {
    const el = nodes[i];
    if (el.children && el.children.length) {
      continue;
    }
    try {
      if (el.closest("button, [role='button'], [role='menu'], [role='menuitem'], [role='tooltip'], [role='dialog'], [role='alert'], [aria-live]")) {
        continue;
      }
    } catch (err) {
      continue;
    }
    const text = String(el.textContent || "").replace(/\s+/g, " ").trim();
    if (!text || text.length > 80 || iconText(el, text)) {
      continue;
    }
    let isMarked = false;
    try {
      isMarked = !!el.closest(".notranslate");
    } catch (err) {
      isMarked = false;
    }
    (isMarked ? marked : plain).push(text);
  }
  return marked.concat(plain);
}

function tileSaysYou(tile) {
  const texts = tileLabelTexts(tile);
  for (const text of texts) {
    if (/^\(?\s*you\s*\)?$/i.test(text)) {
      return true;
    }
  }
  return false;
}

function noteActivity(id, now) {
  let times = tileActivity.get(id);
  if (!times) {
    times = [];
    tileActivity.set(id, times);
  }
  times.push(now);
  while (times.length && now - times[0] > ACTIVITY_WINDOW_MS) {
    times.shift();
  }
  if (times.length > 200) {
    times.splice(0, times.length - 200);
  }
}

function activityScore(id, now) {
  const times = tileActivity.get(id);
  if (!times) {
    return 0;
  }
  while (times.length && now - times[0] > ACTIVITY_WINDOW_MS) {
    times.shift();
  }
  return times.length;
}

function watchTileActivity() {
  if (typeof MutationObserver === "undefined" || !document.body) {
    return;
  }
  const observer = new MutationObserver((mutations) => {
    const now = Date.now();
    const seen = new Set();
    for (const mutation of mutations) {
      let node = mutation.target;
      if (node && node.nodeType !== 1) {
        node = node.parentElement;
      }
      if (!node || !node.closest || node.tagName === "VIDEO") {
        continue;
      }
      let tile = null;
      try {
        tile = node.closest("[data-participant-id]");
      } catch (err) {
        tile = null;
      }
      if (!tile || tile === node) {
        continue;
      }
      const id = tile.getAttribute("data-participant-id") || "";
      // One count per tile per mutation batch, so a single relayout of a
      // tile does not look like a second of speech.
      if (id && !seen.has(id)) {
        seen.add(id);
        noteActivity(id, now);
      }
    }
  });
  observer.observe(document.body, {
    subtree: true,
    attributes: true,
    attributeFilter: ["class", "style", "data-audio-level"],
  });
}

function speakingByActivity(ids) {
  // The tile(s) with clearly more indicator activity than the rest. A layout
  // change touches every tile at once, so require a lead over the quietest.
  const now = Date.now();
  const scores = ids.map((id) => [id, activityScore(id, now)]);
  if (!scores.length) {
    return new Set();
  }
  const values = scores.map((pair) => pair[1]);
  const max = Math.max.apply(null, values);
  const min = Math.min.apply(null, values);
  const out = new Set();
  if (max < ACTIVITY_MIN) {
    return out;
  }
  for (const [id, score] of scores) {
    if (score >= ACTIVITY_MIN && score >= max * 0.6 && (scores.length === 1 || score - min >= ACTIVITY_MIN)) {
      out.add(id);
    }
  }
  return out;
}

function captionRoots() {
  const roots = [];
  const seen = [];
  function add(el) {
    if (!el || seen.indexOf(el) !== -1) {
      return;
    }
    seen.push(el);
    roots.push(el);
  }
  try {
    const regions = document.querySelectorAll("[role='region']");
    for (const el of regions) {
      const label = String(el.getAttribute("aria-label") || "").toLowerCase();
      if (label.indexOf("caption") !== -1 || label.indexOf("subtitle") !== -1) {
        add(el);
      }
    }
  } catch (err) {
    // Meet markup we do not understand contributes no captions.
  }
  const historical = [".a4cQT", ".iOzk7", "[jsname='dsyhDe']"];
  for (const selector of historical) {
    try {
      document.querySelectorAll(selector).forEach(add);
    } catch (err) {
      // An obsolete selector is not an error.
    }
  }
  return roots;
}

function leafTexts(block) {
  const leaves = [];
  let nodes = [];
  try {
    nodes = block.querySelectorAll("div, span");
  } catch (err) {
    return leaves;
  }
  const limit = Math.min(nodes.length, 24);
  for (let i = 0; i < limit; i++) {
    const node = nodes[i];
    if (node.childElementCount > 0) {
      continue;
    }
    try {
      if (node.closest && node.closest("button, [role='button']")) {
        continue;
      }
    } catch (err) {
      continue;
    }
    const raw = String(node.textContent || "").replace(/\s+/g, " ").trim();
    if (!raw || raw.length > 500) {
      continue;
    }
    leaves.push(raw);
  }
  return leaves;
}

function readCaptionBlock(block) {
  if (!block || !block.querySelectorAll) {
    return null;
  }
  let name = "";
  let text = "";
  try {
    const img = block.querySelector("img[alt]");
    if (img) {
      name = cleanLabel(img.getAttribute("alt") || "");
    }
  } catch (err) {
    name = "";
  }
  const nameSelectors = [".KcIKyf", ".NWpY1d", "[data-speaker-name]"];
  const textSelectors = [".ygicle", ".VbkSUe", ".bh44bd", "[data-caption-text]"];
  if (!name) {
    for (const selector of nameSelectors) {
      let el = null;
      try {
        el = block.querySelector(selector);
      } catch (err) {
        el = null;
      }
      if (!el) {
        continue;
      }
      const raw = el.getAttribute("data-speaker-name") || el.textContent || "";
      name = cleanLabel(raw);
      if (name) {
        break;
      }
    }
  }
  for (const selector of textSelectors) {
    let el = null;
    try {
      el = block.querySelector(selector);
    } catch (err) {
      el = null;
    }
    if (!el) {
      continue;
    }
    const raw = String(el.textContent || "").replace(/\s+/g, " ").trim();
    if (raw && raw !== name) {
      text = raw;
      break;
    }
  }
  if (!name || !text) {
    const leaves = leafTexts(block);
    if (!name) {
      for (const raw of leaves) {
        const label = cleanLabel(raw);
        if (label && raw.split(" ").length <= 6) {
          name = label;
          break;
        }
      }
    }
    if (name && !text) {
      let best = "";
      for (const raw of leaves) {
        if (raw === name) {
          continue;
        }
        if (raw.length > best.length) {
          best = raw;
        }
      }
      text = best;
    }
  }
  if (!name || !text || text === name) {
    return null;
  }
  if (!plausibleName(name)) {
    return null;
  }
  return { speaker: name, text: text.slice(0, 600) };
}

function blocksIn(root) {
  let blocks = [];
  try {
    blocks = Array.from(root.querySelectorAll(".nMcdL, .TBMuR"));
  } catch (err) {
    blocks = [];
  }
  if (blocks.length) {
    return blocks;
  }
  const candidates = [];
  const kids = root.children ? Array.from(root.children) : [];
  for (const kid of kids) {
    candidates.push(kid);
    const grands = kid.children ? Array.from(kid.children) : [];
    for (const grand of grands) {
      candidates.push(grand);
    }
  }
  return candidates;
}

function collectCaptions() {
  // Empty when captions are off or the DOM does not match. Never throws.
  const found = [];
  const seen = {};
  let roots = [];
  try {
    roots = captionRoots();
  } catch (err) {
    return [];
  }
  for (const root of roots) {
    let blocks = [];
    try {
      blocks = blocksIn(root);
    } catch (err) {
      continue;
    }
    for (const block of blocks) {
      let item = null;
      try {
        item = readCaptionBlock(block);
      } catch (err) {
        item = null;
      }
      if (!item) {
        continue;
      }
      const key = item.speaker + "\n" + item.text;
      if (seen[key]) {
        continue;
      }
      seen[key] = true;
      found.push(item);
    }
  }
  if (found.length > 6) {
    return found.slice(found.length - 6);
  }
  return found;
}

var lastCaptionText = {};

function activeCaptionSpeaker(captions) {
  // The row Meet is updating is the person talking. A single visible row is
  // that person too. Several stable rows: the last one is the live line.
  if (!captions || !captions.length) {
    return "";
  }
  const changed = [];
  for (const row of captions) {
    if (!row || !row.speaker) {
      continue;
    }
    if (lastCaptionText[row.speaker] !== row.text) {
      changed.push(row.speaker);
    }
  }
  const still = {};
  for (const row of captions) {
    if (row && row.speaker) {
      still[row.speaker] = row.text || "";
    }
  }
  for (const key of Object.keys(lastCaptionText)) {
    if (!Object.prototype.hasOwnProperty.call(still, key)) {
      delete lastCaptionText[key];
    }
  }
  for (const key of Object.keys(still)) {
    lastCaptionText[key] = still[key];
  }
  if (changed.length === 1) {
    return changed[0];
  }
  if (captions.length === 1 && captions[0] && captions[0].speaker) {
    return captions[0].speaker;
  }
  const last = captions[captions.length - 1];
  return (last && last.speaker) || "";
}

function markedSelf(tile) {
  if (!tile) {
    return false;
  }
  if (tile.getAttribute && tile.getAttribute("data-self-name") != null) {
    return true;
  }
  try {
    if (tile.querySelector && tile.querySelector("[data-self-name]")) {
      return true;
    }
  } catch (err) {
    // A tile we cannot search is not the self marker.
  }
  const label = String((tile.getAttribute && tile.getAttribute("aria-label")) || "");
  if (/\(\s*you\s*\)/i.test(label)) {
    return true;
  }
  const text = String(tile.textContent || "");
  if (/\(\s*you\s*\)/i.test(text)) {
    return true;
  }
  return tileSaysYou(tile);
}

function selfIdentity() {
  const el = document.querySelector("[data-self-name]");
  let name = el ? cleanLabel(el.getAttribute("data-self-name") || el.textContent || "") : "";
  let id = "";
  let node = el;
  while (node) {
    if (node.getAttribute && node.getAttribute("data-participant-id")) {
      id = node.getAttribute("data-participant-id");
      break;
    }
    node = node.parentElement;
  }
  if (!id) {
    let tiles = [];
    try {
      tiles = document.querySelectorAll("[data-participant-id]");
    } catch (err) {
      tiles = [];
    }
    for (const tile of tiles) {
      if (!markedSelf(tile)) {
        continue;
      }
      id = tile.getAttribute("data-participant-id") || "";
      if (!name) {
        name = nameFromTile(tile);
      }
      break;
    }
  }
  return { name, id };
}

function nameFromTile(tile) {
  // Only the tile's own label. Buttons, menus, and toasts inside the tile
  // ("Reframe", "Font size", "Others might still see your full video.") are
  // not names, so descendant text is ignored.
  const ranked = [];
  function add(text) {
    const name = cleanLabel(text);
    if (name && ranked.indexOf(name) === -1) {
      ranked.push(name);
    }
  }
  try {
    const selfNode = tile.querySelector("[data-self-name]");
    if (selfNode) {
      add(selfNode.getAttribute("data-self-name") || "");
    }
  } catch (err) {
    // Fall through to the tile's own aria-label.
  }
  add(tile.getAttribute && tile.getAttribute("aria-label"));
  for (const text of tileLabelTexts(tile)) {
    add(text);
  }
  const people = ranked.filter((name) => name.toLowerCase() !== "you");
  return people[0] || "";
}

function meetPresence() {
  // In-call is the leave-call control. The lobby has Join now and no leave
  // button. A hidden tab is still in the call; visibility is reported, not
  // required.
  let code = "";
  try {
    const path = String((location && location.pathname) || "");
    const match = path.match(/\/([a-z]{3}-[a-z]{4}-[a-z]{3})(?:\/|$)/i);
    if (match) {
      code = match[1].toLowerCase();
    }
  } catch (err) {
    code = "";
  }
  let leave = false;
  let lobby = false;
  let buttons = [];
  try {
    buttons = document.querySelectorAll("button, [role='button']");
  } catch (err) {
    buttons = [];
  }
  const limit = Math.min(buttons.length, 120);
  for (let i = 0; i < limit; i++) {
    const button = buttons[i];
    const label = String(
      (button.getAttribute && button.getAttribute("aria-label")) || button.textContent || ""
    ).toLowerCase();
    if (
      label.indexOf("leave call") !== -1 ||
      label.indexOf("leave meeting") !== -1 ||
      label.indexOf("hang up") !== -1 ||
      label.indexOf("end call") !== -1 ||
      label === "leave"
    ) {
      leave = true;
    }
    if (label.indexOf("join now") !== -1 || label.indexOf("ask to join") !== -1) {
      lobby = true;
    }
  }
  // Tiles with data-participant-id mean the call UI is up. Do not treat a
  // live call as the lobby just because the leave button's label changed.
  let tiles = 0;
  try {
    tiles = document.querySelectorAll("[data-participant-id]").length;
  } catch (err) {
    tiles = 0;
  }
  let phase = "left";
  if (leave || tiles > 0) {
    phase = "in-call";
  } else if (lobby || code) {
    phase = "lobby";
  }
  let visible = true;
  try {
    visible = document.visibilityState !== "hidden";
  } catch (err) {
    visible = true;
  }
  let title = "";
  try {
    title = document.title || "";
  } catch (err) {
    title = "";
  }
  const people = [];
  try {
    const snapshot = collect();
    for (const person of snapshot.participants || []) {
      if (person && person.name) {
        people.push({ name: person.name, self: !!person.self });
      }
    }
  } catch (err) {
    // Presence still reports the code and phase without names.
  }
  return {
    meet_code: code,
    in_call: phase === "in-call",
    phase: phase,
    visible: visible,
    title: title,
    participants: people,
  };
}

function captionsEnabled() {
  // "on" when the CC control says captions are showing, or a captions region
  // is open. "off" only when that control says they are hidden. "" when Meet
  // does not expose the control. No caption text is not the same as off:
  // a call where nobody else speaks has no lines.
  let buttons = [];
  try {
    buttons = document.querySelectorAll("button, [role='button']");
  } catch (err) {
    buttons = [];
  }
  const limit = Math.min(buttons.length, 80);
  for (let i = 0; i < limit; i++) {
    const button = buttons[i];
    const label = String((button.getAttribute && button.getAttribute("aria-label")) || "").toLowerCase();
    if (label.indexOf("caption") === -1 && label.indexOf("subtitle") === -1) {
      continue;
    }
    const pressed = String((button.getAttribute && button.getAttribute("aria-pressed")) || "").toLowerCase();
    if (pressed === "true") {
      return true;
    }
    if (pressed === "false") {
      return false;
    }
    if (label.indexOf("turn off") !== -1 || label.indexOf("hide caption") !== -1) {
      return true;
    }
    if (label.indexOf("turn on") !== -1 || label.indexOf("show caption") !== -1) {
      return false;
    }
  }
  try {
    if (captionRoots().length) {
      return true;
    }
  } catch (err) {
    return null;
  }
  return null;
}

function collect() {
  const self = selfIdentity();
  const byId = new Map();
  for (const el of document.querySelectorAll("[data-participant-id]")) {
    const id = el.getAttribute("data-participant-id") || "";
    if (!id) {
      continue;
    }
    const name = nameFromTile(el);
    const speaking = looksSpeaking(el);
    const isSelf =
      (self.id && id === self.id) ||
      (self.name && name && name === self.name) ||
      markedSelf(el);
    const prev = byId.get(id) || { id, name: "", self: isSelf, speaking: false };
    prev.speaking = prev.speaking || speaking;
    prev.self = prev.self || isSelf;
    if (name && name.length > (prev.name || "").length) {
      prev.name = name;
    }
    byId.set(id, prev);
  }

  const busy = speakingByActivity(Array.from(byId.keys()));
  for (const person of byId.values()) {
    if (busy.has(person.id)) {
      person.speaking = true;
    }
  }

  const participants = [];
  const active = [];
  for (const person of byId.values()) {
    if (person.self) {
      if (!self.name && person.name) {
        self.name = person.name;
      }
      if (person.speaking) {
        active.push({
          id: person.id,
          name: self.name || person.name || "",
          self: true,
        });
      }
      continue;
    }
    if (person.name) {
      participants.push({ id: person.id, name: person.name, self: false });
    }
    if (person.speaking) {
      active.push({ id: person.id, name: person.name || "", self: false });
    }
  }
  if (self.name) {
    participants.unshift({ id: self.id || "", name: self.name, self: true });
  }
  let captions = [];
  try {
    captions = collectCaptions();
  } catch (err) {
    captions = [];
  }
  let captionSpeaker = "";
  try {
    captionSpeaker = activeCaptionSpeaker(captions);
  } catch (err) {
    captionSpeaker = "";
  }
  return {
    inCall: byId.size > 0 || Boolean(self.name),
    active,
    participants,
    self: self.name || "",
    captions,
    captionSpeaker,
    captionsEnabled: captionsEnabled(),
  };
}

var lastKey = "";
var lastSent = 0;

function sample() {
  let snapshot;
  try {
    snapshot = collect();
  } catch (err) {
    return;
  }
  if (!snapshot.inCall) {
    return;
  }
  const payload = {
    active: snapshot.active,
    participants: snapshot.participants,
    self: snapshot.self,
    caption_speaker: snapshot.captionSpeaker || "",
  };
  if (snapshot.captionsEnabled === true || snapshot.captionsEnabled === false) {
    payload.captions_enabled = snapshot.captionsEnabled;
  }
  if (snapshot.captions && snapshot.captions.length) {
    payload.captions = snapshot.captions;
  }
  const key = JSON.stringify(payload);
  const now = Date.now();
  if (key === lastKey && now - lastSent < 1000) {
    return;
  }
  lastKey = key;
  lastSent = now;
  let viaWorker = false;
  try {
    if (chrome.runtime && chrome.runtime.id) {
      chrome.runtime.sendMessage({ type: "speakers", payload });
      viaWorker = true;
    }
  } catch (err) {
    // The bridge is down whenever nothing is recording. That is expected.
  }
  if (!viaWorker) {
    // After an extension reload the old worker is gone. Post directly.
    try {
      fetch("http://127.0.0.1:" + BRIDGE_PORT + "/speakers", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      }).catch(function () {});
    } catch (err) {}
  }
}

function sendPresence() {
  let payload;
  try {
    payload = meetPresence();
  } catch (err) {
    return;
  }
  try {
    chrome.runtime.sendMessage({ type: "presence", payload: payload });
  } catch (err) {
    // The watcher is down. The next heartbeat tries again.
  }
  // Fallback if the service worker is asleep or Chrome blocks the SW fetch.
  try {
    fetch("http://127.0.0.1:" + PRESENCE_PORT + "/presence", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    }).catch(function () {});
  } catch (err) {}
}

// Top-level bindings are var so a second injection into the same world
// cannot fail with a redeclaration error.
// The worker re-injects this file into open Meet tabs after an update, so a
// reload during a call takes effect without refreshing the call. Only one
// copy of a given version runs per tab.
var ALREADY_RUNNING =
  typeof window !== "undefined" && window.__meetingNotesSpeakerTags === SCRIPT_VERSION;
if (typeof window !== "undefined") {
  window.__meetingNotesSpeakerTags = SCRIPT_VERSION;
}

if (!ALREADY_RUNNING && typeof chrome !== "undefined" && chrome.runtime && chrome.runtime.sendMessage) {
  try {
    watchTileActivity();
  } catch (err) {
    // Without the observer, the older speaking signals still apply.
  }
  setInterval(sample, 400);
  setInterval(sendPresence, 3000);
  sample();
  sendPresence();
}
