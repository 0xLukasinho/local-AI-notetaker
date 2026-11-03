// Reads Google Meet participant names and the active-speaker highlight.
// Posts them to the local meeting-notes bridge. Does not change the page,
// so other people in the call cannot see it.
//
// Meet obfuscates class names. Detection uses, in order:
//   data-participant-id, data-self-name, data-audio-level,
//   a few historically stable speaking classes, and a thick tile border
//   (the active-speaker ring). Names come from leaf text and aria-labels.

const SPEAKING_CLASSES = ["Oaajhc", "HX2H7", "wEsLMd", "OgVli"];
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

function looksSpeaking(root) {
  if (!root || !root.getAttribute) {
    return false;
  }
  const level = root.getAttribute("data-audio-level");
  if (level && level !== "0") {
    return true;
  }
  for (const name of SPEAKING_CLASSES) {
    if (root.classList && root.classList.contains(name)) {
      return true;
    }
  }
  if (thickBorder(root)) {
    return true;
  }
  if (root.querySelector("[data-audio-level]:not([data-audio-level='0'])")) {
    return true;
  }
  for (const name of SPEAKING_CLASSES) {
    if (root.querySelector("." + name)) {
      return true;
    }
  }
  const nodes = root.querySelectorAll("div");
  const limit = Math.min(nodes.length, 40);
  for (let i = 0; i < limit; i++) {
    if (thickBorder(nodes[i])) {
      return true;
    }
  }
  return false;
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
  return {
    inCall: byId.size > 0 || Boolean(self.name),
    active,
    participants,
    self: self.name || "",
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
  };
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

setInterval(sample, 400);
sample();
