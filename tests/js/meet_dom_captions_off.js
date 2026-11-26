// Runs content.js against a Meet-shaped DOM with captions off and no
// aria-label, data-self-name, or data-audio-level on the tiles (the Meet UI
// in which v1.4.0 read nothing). Prints the last speaker sample as JSON.
const fs = require("fs");
const path = require("path");
const { JSDOM } = require("jsdom");

const html = `<!doctype html><html><body>
<div class="grid">
  <div data-participant-id="spaces/x/devices/1" class="tile">
    <video></video>
    <div class="name-wrap"><span class="notranslate">Alex Rivera</span></div>
    <button aria-label="More options for Alex Rivera"><i class="icon">more_vert</i></button>
    <div class="indicator"><div class="bar"></div></div>
  </div>
  <div data-participant-id="spaces/x/devices/2" class="tile">
    <video></video>
    <div class="name-wrap"><span class="notranslate">Priya Shah</span></div>
    <div role="alert"><span>Priya Shah joined</span></div>
    <div class="indicator"><div class="bar"></div></div>
  </div>
  <div data-participant-id="spaces/x/devices/3" class="tile">
    <video></video>
    <div class="name-wrap"><span class="notranslate">You</span></div>
    <div class="indicator"><div class="bar"></div></div>
  </div>
</div>
<button aria-label="Turn on captions" aria-pressed="false">cc</button>
<button aria-label="Leave call">call_end</button>
</body></html>`;

const dom = new JSDOM(html, { url: "https://meet.google.com/abc-defg-hij", runScripts: "outside-only" });
const sent = [];
dom.window.chrome = {
  runtime: {
    id: "test",
    sendMessage: (message) => {
      if (message && message.type === "speakers") {
        sent.push(message.payload);
      }
    },
  },
};
dom.window.fetch = () => Promise.resolve({});
const source = fs.readFileSync(
  path.join(__dirname, "..", "..", "meeting_notes", "chrome_extension", "content.js"),
  "utf8"
);
dom.window.eval(source);
// Loading the file twice (re-injection after an update) must not throw or
// start a second sampler.
dom.window.eval(source);

const speaker = process.argv[2] || "1";
const bar = dom.window.document.querySelector(`[data-participant-id="spaces/x/devices/${speaker}"] .bar`);
let n = 0;
const timer = setInterval(() => {
  n += 1;
  bar.className = "bar level-" + (n % 5);
}, 60);
setTimeout(() => {
  clearInterval(timer);
  process.stdout.write(JSON.stringify(sent[sent.length - 1] || null));
  process.exit(0);
}, 1600);
