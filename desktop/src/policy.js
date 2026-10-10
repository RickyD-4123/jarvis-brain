'use strict';
// Which URLs the desk window may load. One rule, used by both the Chrome desk window and the Electron
// fallback window: only the backend's own origin (http://127.0.0.1:<port>). Anything else is never
// loaded in the app; http(s) links are handed to your default browser instead, other schemes dropped.

function localOrigin(port) {
  return `http://127.0.0.1:${port}`;
}

function parse(url) {
  try {
    return new URL(String(url));
  } catch {
    return null;
  }
}

/** True only for a URL on exactly this origin (same scheme, host and port). */
function isLocal(url, origin) {
  const u = parse(url);
  return !!u && u.origin === origin;
}

/** True for a link that may be opened in the default browser (http/https only). */
function isExternalOk(url) {
  const u = parse(url);
  return !!u && (u.protocol === 'http:' || u.protocol === 'https:');
}

/**
 * What to do with a navigation: "allow" (local), "external" (open in the default browser) or "block".
 * about:blank is allowed so a blank popup can be inspected and closed.
 */
function decide(url, origin) {
  if (isLocal(url, origin)) return 'allow';
  if (url === 'about:blank') return 'allow';
  return isExternalOk(url) ? 'external' : 'block';
}

module.exports = { localOrigin, isLocal, isExternalOk, decide };
