const BRIDGE_PORT = 8765;

chrome.runtime.onMessage.addListener((message) => {
  if (!message) {
    return;
  }
  if (message.type === "speakers") {
    const payload = message.payload || {};
    fetch(`http://127.0.0.1:${BRIDGE_PORT}/speakers`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    }).catch(() => {});
    return;
  }
  if (message.type === "presence") {
    const payload = message.payload || {};
    fetch(`http://127.0.0.1:${BRIDGE_PORT + 1}/presence`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    }).catch(() => {});
  }
});
