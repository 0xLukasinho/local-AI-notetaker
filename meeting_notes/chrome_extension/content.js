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
const AUDIO_LEVEL_FLOOR = 8;
const BLOCKED_NAMES = [
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

function plausibleName(text) {
  if (!text || text.length < 2 || text.length > 80) {
    return false;
  }
  if (!/\p{L}/u.test(text)) {
    return false;
  }
  const lower = text.toLowerCase();
  for (const word of BLOCKED_NAMES) {
    if (lower === word || lower.startsWith(word + " ")) {
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

const lastCaptionText = {};

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

function selfIdentity() {
  const el = document.querySelector("[data-self-name]");
  const name = el ? (el.getAttribute("data-self-name") || "").trim() : "";
  let id = "";
  let node = el;
  while (node) {
    if (node.getAttribute && node.getAttribute("data-participant-id")) {
      id = node.getAttribute("data-participant-id");
      break;
    }
    node = node.parentElement;
  }
  return { name, id };
}

function nameFromTile(tile) {
  const aria = cleanLabel(tile.getAttribute && tile.getAttribute("aria-label"));
  const texts = [];
  if (aria) {
    texts.push(aria);
  }
  const nodes = tile.querySelectorAll("span, div");
  for (const node of nodes) {
    if (node.closest("button, [role='button']")) {
      continue;
    }
    if (node.childElementCount > 0) {
      continue;
    }
    const text = cleanLabel(node.textContent || "");
    if (text) {
      texts.push(text);
    }
  }
  if (!texts.length) {
    return "";
  }
  texts.sort((a, b) => b.length - a.length);
  return texts[0];
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
    const isSelf = (self.id && id === self.id) || (self.name && name === self.name);
    const prev = byId.get(id) || { id, name: "", self: isSelf, speaking: false };
    prev.speaking = prev.speaking || speaking;
    prev.self = prev.self || isSelf;
    if (name && name.length > (prev.name || "").length) {
      prev.name = name;
    }
    byId.set(id, prev);
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
  };
}

let lastKey = "";
let lastSent = 0;

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
  try {
    chrome.runtime.sendMessage({ type: "speakers", payload });
  } catch (err) {
    // The bridge is down whenever nothing is recording. That is expected.
  }
}

if (typeof chrome !== "undefined" && chrome.runtime && chrome.runtime.sendMessage) {
  setInterval(sample, 400);
  sample();
}
