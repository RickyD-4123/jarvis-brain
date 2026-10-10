'use strict';
// A throwaway local web server standing in for the backend in the window tests.
const http = require('http');

const PAGES = {
  '/': '<!doctype html><title>local</title><p id="ok">local page</p>',
  '/go-external': '<!doctype html><title>go</title><script>setTimeout(() => { location.href = "https://example.com/navigated"; }, 50);</script>',
  '/go-file': '<!doctype html><title>file</title><script>setTimeout(() => { location.href = "file:///C:/Windows/win.ini"; }, 50);</script>',
};

function startTestServer() {
  return new Promise((resolve) => {
    const srv = http.createServer((req, res) => {
      const route = req.url.split('?')[0];
      if (route === '/redir') {
        res.writeHead(302, { Location: 'https://example.com/redirected' });
        res.end();
        return;
      }
      const body = PAGES[route];
      res.writeHead(body ? 200 : 404, { 'Content-Type': 'text/html; charset=utf-8' });
      res.end(body || 'not found');
    });
    srv.listen(0, '127.0.0.1', () => resolve({ srv, port: srv.address().port, origin: `http://127.0.0.1:${srv.address().port}` }));
  });
}

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function until(fn, ms = 10000, step = 100) {
  const end = Date.now() + ms;
  while (Date.now() < end) {
    const v = await fn();
    if (v) return v;
    await sleep(step);
  }
  return fn();
}

module.exports = { startTestServer, sleep, until };
