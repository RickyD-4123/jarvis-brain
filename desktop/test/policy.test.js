'use strict';
const test = require('node:test');
const assert = require('node:assert');
const policy = require('../src/policy');

const origin = policy.localOrigin(4700);

test('only the exact local origin is allowed', () => {
  for (const ok of ['http://127.0.0.1:4700/', 'http://127.0.0.1:4700/index.html?x=1', 'http://127.0.0.1:4700/#a']) {
    assert.strictEqual(policy.decide(ok, origin), 'allow', ok);
  }
  for (const bad of ['http://127.0.0.1:4701/', 'https://127.0.0.1:4700/', 'http://localhost:4700/', 'http://127.0.0.1.evil.example:4700/',
    'http://evil.example/?http://127.0.0.1:4700/', 'http://127.0.0.1:4700@evil.example/', 'http://[::1]:4700/']) {
    assert.notStrictEqual(policy.decide(bad, origin), 'allow', bad);
  }
});

test('http(s) elsewhere goes to the default browser; other schemes are dropped', () => {
  assert.strictEqual(policy.decide('https://mail.google.com/x', origin), 'external');
  assert.strictEqual(policy.decide('http://example.com/', origin), 'external');
  for (const bad of ['file:///C:/Windows/win.ini', 'javascript:alert(1)', 'data:text/html,<b>x</b>', 'ms-settings:', 'chrome://settings', 'not a url']) {
    assert.strictEqual(policy.decide(bad, origin), 'block', bad);
  }
});
