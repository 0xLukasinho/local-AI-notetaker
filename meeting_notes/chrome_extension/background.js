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

// After an install or reload, content scripts in Meet tabs that were already
// open belong to the old version and cannot reach this worker. Inject the new
// content.js into them so an update applies mid-call without a page refresh.
function injectIntoOpenMeetTabs() {
  if (!chrome.scripting || !chrome.tabs) {
    return;
  }
  chrome.tabs.query({ url: "https://meet.google.com/*" }, (tabs) => {
    for (const tab of tabs || []) {
      if (!tab || tab.id == null) {
        continue;
      }
      chrome.scripting
        .executeScript({ target: { tabId: tab.id }, files: ["content.js"] })
        .catch(() => {});
    }
  });
}

chrome.runtime.onInstalled.addListener(injectIntoOpenMeetTabs);
