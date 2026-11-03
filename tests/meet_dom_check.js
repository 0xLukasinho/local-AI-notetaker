// Exercises the Meet content script's speaking heuristic and caption reader
// against a tiny fake DOM. Run with: node tests/meet_dom_check.js

const fs = require("fs");
const path = require("path");
const vm = require("vm");

function matches(node, selector) {
  const sel = selector.trim();
  if (!sel) {
    return false;
  }
  if (sel.includes(",")) {
    return sel.split(",").some((part) => matches(node, part));
  }
  let rest = sel;
  if (rest.startsWith(".")) {
    const space = rest.indexOf(" ");
    const token = space === -1 ? rest : rest.slice(0, space);
    const name = token.slice(1);
    const classes = String(node.attrs.class || "").split(/\s+/);
    if (!classes.includes(name)) {
      return false;
    }
    rest = space === -1 ? "" : rest.slice(space).trim();
    if (!rest) {
      return true;
    }
  }
  const bracket = rest.indexOf("[");
  if (bracket === 0 || (bracket > 0 && !rest.slice(0, bracket).includes(" "))) {
    const tag = bracket > 0 ? rest.slice(0, bracket) : "";
    if (tag && node.tag !== tag) {
      return false;
    }
    const end = rest.indexOf("]");
    const body = rest.slice(bracket + 1, end);
    const eq = body.indexOf("=");
    if (eq === -1) {
      return Object.prototype.hasOwnProperty.call(node.attrs, body);
    }
    const key = body.slice(0, eq).trim();
    const want = body.slice(eq + 1).trim().replace(/^['"]|['"]$/g, "");
    return String(node.attrs[key]) === want;
  }
  if (rest && !rest.startsWith("[") && !rest.startsWith(".")) {
    return node.tag === rest;
  }
  return false;
}

function queryAll(node, selector) {
  const found = [];
  function walk(current) {
    for (const child of current.children || []) {
      if (matches(child, selector)) {
        found.push(child);
      }
      walk(child);
    }
  }
  walk(node);
  return found;
}

function el(tag, attrs, children, text) {
  const node = {
    tag,
    attrs: attrs || {},
    children: children || [],
    ownerDocument: {},
    parentElement: null,
    style: (attrs && attrs.style) || {
      borderTopWidth: "0px",
      borderTopColor: "rgba(0, 0, 0, 1)",
    },
  };
  node.childElementCount = node.children.length;
  node.classList = {
    contains(name) {
      return String(node.attrs.class || "").split(/\s+/).includes(name);
    },
  };
  node.getAttribute = (name) =>
    Object.prototype.hasOwnProperty.call(node.attrs, name) ? String(node.attrs[name]) : null;
  node.querySelectorAll = (selector) => queryAll(node, selector);
  node.querySelector = (selector) => queryAll(node, selector)[0] || null;
  node.closest = (selector) => {
    let current = node;
    while (current) {
      if (matches(current, selector)) {
        return current;
      }
      current = current.parentElement;
    }
    return null;
  };
  for (const child of node.children) {
    child.parentElement = node;
  }
  if (text) {
    node.textContent = text;
  } else {
    node.textContent = node.children
      .map((child) => child.textContent || "")
      .join(" ")
      .trim();
  }
  return node;
}

function assert(cond, message) {
  if (!cond) {
    console.error("FAIL:", message);
    process.exit(1);
  }
}

const root = el("div", {}, []);
const document = {
  querySelector(selector) {
    return queryAll(root, selector)[0] || null;
  },
  querySelectorAll(selector) {
    return queryAll(root, selector);
  },
};
root.children = [];

const context = {
  document,
  console,
  setInterval() {},
  getComputedStyle(node) {
    return node.style;
  },
  isFinite,
  parseFloat,
  String,
  Object,
  Array,
  Math,
};
context.globalThis = context;
vm.createContext(context);
const source = fs.readFileSync(
  path.join(__dirname, "..", "meeting_notes", "chrome_extension", "content.js"),
  "utf8"
);
vm.runInContext(source, context);

const thick = {
  borderTopWidth: "4px",
  borderTopColor: "rgb(26, 115, 232)",
};
const idle = el("div", { class: "Oaajhc", "data-audio-level": "1" }, [
  el("div", { style: thick }, []),
]);
assert(context.looksSpeaking(idle) === false, "inner border and a low level must not mark every tile");

const ring = el("div", { style: thick, "data-audio-level": "0" }, []);
assert(context.looksSpeaking(ring) === true, "a saturated ring on the tile itself is speaking");

const loud = el("div", { "data-audio-level": "9" }, []);
assert(context.looksSpeaking(loud) === true, "a high audio level is speaking");

const quiet = el("div", { "data-audio-level": "1" }, []);
assert(context.looksSpeaking(quiet) === false, "audio level 1 is the idle floor");

const flagged = el("div", { "data-is-speaking": "true" }, []);
assert(context.looksSpeaking(flagged) === true, "data-is-speaking is speaking");

assert(context.collectCaptions().length === 0, "captions off yields nothing");

const captionBlock = el("div", { class: "nMcdL" }, [
  el("img", { alt: "Daniel Bronheim" }, []),
  el("div", { class: "KcIKyf" }, [], "Daniel Bronheim"),
  el("div", { class: "ygicle" }, [], "we should ship friday"),
]);
const region = el(
  "div",
  { role: "region", "aria-label": "Captions" },
  [captionBlock]
);
root.children = [region];
captionBlock.parentElement = region;
region.parentElement = root;

const captions = context.collectCaptions();
assert(captions.length === 1, "one caption row: " + JSON.stringify(captions));
assert(captions[0].speaker === "Daniel Bronheim", "caption speaker");
assert(captions[0].text === "we should ship friday", "caption text");

root.children = [el("div", { role: "region", "aria-label": "Captions" }, [
  el("button", {}, [], "Turn on captions"),
])];
assert(context.collectCaptions().length === 0, "chrome in the caption region is ignored");

const broken = {
  querySelectorAll() {
    throw new Error("meet changed");
  },
  querySelector() {
    throw new Error("meet changed");
  },
  getAttribute() {
    return null;
  },
  children: [],
};
assert(context.looksSpeaking(broken) === false, "a hostile tile does not throw");
let threw = false;
try {
  context.readCaptionBlock(broken);
} catch (err) {
  threw = true;
}
assert(threw === false, "caption parsing stays defensive");

console.log("meet dom checks ok");
