const BRIDGE_PORT = 8765;

chrome.runtime.onMessage.addListener((message) => {
  if (!message || message.type !== "speakers") {
    return;
  }
  const payload = message.payload || {};
  fetch(`http://127.0.0.1:${BRIDGE_PORT}/speakers`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  }).catch(() => {});
});
