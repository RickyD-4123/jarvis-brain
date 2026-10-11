#!/usr/bin/env python3
"""Preflight: run every live chain end to end against the running local system and print a verdict.

    python preflight.py            (the server must already be running: python server.py)

Real calls only: real HTTP to the server, real `claude` subprocesses, a real file written by
/remember, a real JPEG through /see. Standard library only. Exits 1 if anything fails.

Side effects, all cleaned up: /remember writes one probe note, which is deleted once it has been
read back (its star stays in the galaxy until the server restarts), and the chat history is reset
at the end so the probe questions can't leak into your follow-ups.
"""
import base64
import hashlib
import http.client
import json
import math
import os
import random
import re
import socket
import subprocess
import sys
import time
import urllib.parse

import server

HOST, PORT = server.HOST, server.PORT
ROOT = server.ROOT
VIEWER_DIR = server.VIEWER_DIR

results = []   # (status, name, detail): status is "pass" | "fail" | "warn"


def report(status, name, detail=""):
    results.append((status, name, detail))
    mark = {"pass": "✔", "fail": "✘", "warn": "!"}[status]
    print(f" {mark} {status.upper():4}  {name}" + (f"\n          {detail}" if detail else ""), flush=True)


def note(text):
    print(f"          {text}", flush=True)


# ---------------------------------------------------------------- http

OWN_ORIGIN = f"http://{HOST}:{PORT}"


def request(method, path, body=None, headers=None, timeout=240):
    """Raw HTTP: the path goes on the wire exactly as given (no client-side normalising of '..').
    POSTs carry our own Origin, as the page's do, unless the caller sets one (or None to omit it)."""
    headers = dict(headers or {})
    headers.setdefault("X-Jarvis-Probe", "1")             # so the probes never teach Jarvis's memory anything
    if method == "POST" and "Origin" not in headers:
        headers["Origin"] = OWN_ORIGIN
    headers = {k: v for k, v in headers.items() if v is not None}
    conn = http.client.HTTPConnection(HOST, PORT, timeout=timeout)
    try:
        conn.request(method, path, body=body, headers=headers)
        res = conn.getresponse()
        return res.status, res.getheader("Content-Type", ""), res.read()
    finally:
        conn.close()


def raw_exchange(head, body=b"", timeout=10):
    """Send raw bytes (request line + headers [+ body]) and return (status, headers dict, seconds)."""
    t0 = time.time()
    with socket.create_connection((HOST, PORT), timeout=timeout) as sock:
        sock.sendall(head.encode("latin-1") + body)
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = sock.recv(4096)
            if not chunk:
                break
            data += chunk
    lines = data.split(b"\r\n\r\n")[0].decode("latin-1").split("\r\n")
    status = int(lines[0].split()[1]) if lines and lines[0].startswith("HTTP/") else 0
    hdrs = {l.split(":", 1)[0].strip().lower(): l.split(":", 1)[1].strip() for l in lines[1:] if ":" in l}
    return status, hdrs, time.time() - t0


def post_json(path, payload):
    status, _, raw = request("POST", path, json.dumps(payload).encode("utf-8"),
                             {"Content-Type": "application/json"})
    try:
        return status, json.loads(raw)
    except ValueError:
        return status, {"error": f"not JSON: {raw[:120]!r}"}


def short(text, n=110):
    text = " ".join(str(text).split())
    return text if len(text) <= n else text[:n - 1] + "…"


# ---------------------------------------------------------------- a tiny baseline JPEG encoder
# Grayscale, standard luminance tables (ITU T.81 Annex K). Enough to send a real JPEG to /see.

LUMA_Q = [16, 11, 10, 16, 24, 40, 51, 61, 12, 12, 14, 19, 26, 58, 60, 55,
          14, 13, 16, 24, 40, 57, 69, 56, 14, 17, 22, 29, 51, 87, 80, 62,
          18, 22, 37, 56, 68, 109, 103, 77, 24, 35, 55, 64, 81, 104, 113, 92,
          49, 64, 78, 87, 103, 121, 120, 101, 72, 92, 95, 98, 112, 100, 103, 99]
DC_BITS = [0, 1, 5, 1, 1, 1, 1, 1, 1, 0, 0, 0, 0, 0, 0, 0]
DC_VALS = list(range(12))
AC_BITS = [0, 2, 1, 3, 3, 2, 4, 3, 5, 5, 4, 4, 0, 0, 1, 0x7D]
AC_VALS = bytes.fromhex(
    "01020300041105122131410613516107227114328191a1082342b1c11552d1f0"
    "2433627282090a161718191a25262728292a3435363738393a43444546474849"
    "4a535455565758595a636465666768696a737475767778797a83848586878889"
    "8a92939495969798999aa2a3a4a5a6a7a8a9aab2b3b4b5b6b7b8b9bac2c3c4c5"
    "c6c7c8c9cad2d3d4d5d6d7d8d9dae1e2e3e4e5e6e7e8e9eaf1f2f3f4f5f6f7f8"
    "f9fa")
ZIGZAG = [r * 8 + c for s in range(15)
          for r, c in sorted(((r, s - r) for r in range(max(0, s - 7), min(s, 7) + 1)),
                             reverse=(s % 2 == 0))]
DCT = [[(math.sqrt(0.5) if u == 0 else 1.0) / 2 * math.cos((2 * x + 1) * u * math.pi / 16)
        for x in range(8)] for u in range(8)]


def huffman_codes(bits, vals):
    codes, code, k = {}, 0, 0
    for length in range(1, 17):
        for _ in range(bits[length - 1]):
            codes[vals[k]] = (code, length)
            code, k = code + 1, k + 1
        code <<= 1
    return codes


def encode_jpeg(pixels, w, h, quality=85):
    """pixels: list of rows of 0..255 grey values; w and h must be multiples of 8."""
    scale = 5000 // quality if quality < 50 else 200 - 2 * quality
    q = [max(1, min(255, (v * scale + 50) // 100)) for v in LUMA_Q]
    dc_codes, ac_codes = huffman_codes(DC_BITS, DC_VALS), huffman_codes(AC_BITS, AC_VALS)
    out, acc, nbits = bytearray(), 0, 0

    def put(value, length):
        nonlocal acc, nbits
        acc, nbits = (acc << length) | (value & ((1 << length) - 1)), nbits + length
        while nbits >= 8:
            byte = (acc >> (nbits - 8)) & 0xFF
            out.append(byte)
            if byte == 0xFF:
                out.append(0)                              # byte stuffing
            nbits -= 8

    def magnitude(v):
        cat = abs(v).bit_length()
        return cat, (v if v >= 0 else v + (1 << cat) - 1)

    prev_dc = 0
    for by in range(0, h, 8):
        for bx in range(0, w, 8):
            block = [[pixels[by + y][bx + x] - 128 for x in range(8)] for y in range(8)]
            rows = [[sum(DCT[v][x] * block[y][x] for x in range(8)) for v in range(8)] for y in range(8)]
            coef = [sum(DCT[u][y] * rows[y][v] for y in range(8)) for u in range(8) for v in range(8)]
            zz = [round(coef[i] / q[i]) for i in ZIGZAG]
            cat, bits = magnitude(zz[0] - prev_dc)
            prev_dc = zz[0]
            put(*dc_codes[cat])
            if cat:
                put(bits, cat)
            run = 0
            for v in zz[1:]:
                if v == 0:
                    run += 1
                    continue
                while run > 15:
                    put(*ac_codes[0xF0])
                    run -= 16
                cat, bits = magnitude(v)
                put(*ac_codes[(run << 4) | cat])
                put(bits, cat)
                run = 0
            if run:
                put(*ac_codes[0x00])                       # end of block
    if nbits:
        put(0x7F, 8 - nbits)                               # pad with 1s

    def segment(marker, payload):
        return bytes([0xFF, marker]) + (len(payload) + 2).to_bytes(2, "big") + payload

    dqt = bytes([0]) + bytes(q[i] for i in ZIGZAG)
    sof = bytes([8]) + h.to_bytes(2, "big") + w.to_bytes(2, "big") + bytes([1, 1, 0x11, 0])
    dht = bytes([0x00]) + bytes(DC_BITS) + bytes(DC_VALS) + bytes([0x10]) + bytes(AC_BITS) + AC_VALS
    sos = bytes([1, 1, 0x00, 0, 63, 0])
    return (b"\xff\xd8" + segment(0xE0, b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00")
            + segment(0xDB, dqt) + segment(0xC0, sof) + segment(0xC4, dht) + segment(0xDA, sos)
            + bytes(out) + b"\xff\xd9")


def draw_shape(shape, w=320, h=240):
    """A big black shape on white, centred."""
    cx, cy, r = w / 2, h / 2, 80
    def inside(x, y):
        if shape == "circle":
            return (x - cx) ** 2 + (y - cy) ** 2 <= r * r
        if shape == "square":
            return abs(x - cx) <= r * 0.85 and abs(y - cy) <= r * 0.85
        top, base = cy - r, cy + r * 0.8                  # triangle, point up
        if not top <= y <= base:
            return False
        half = (y - top) / (base - top) * r
        return abs(x - cx) <= half
    return [[0 if inside(x + 0.5, y + 0.5) else 255 for x in range(w)] for y in range(h)]


# ---------------------------------------------------------------- checks

def check_server():
    try:
        status, ctype, body = request("GET", "/", timeout=5)
    except OSError as e:
        report("fail", "Local server is up and serving the viewer",
               f"nothing on http://{HOST}:{PORT} ({e}). Start it with: python server.py")
        return False
    ok = status == 200 and "text/html" in ctype and b'src="graph-data.js"' in body
    report("pass" if ok else "fail", "Local server is up and serving the viewer",
           f"GET / -> {status} {ctype}, {len(body)} bytes" + ("" if ok else " (not the galaxy viewer)"))
    return ok


def load_served_graph():
    status, _, body = request("GET", "/graph-data.js", timeout=10)
    if status != 200:
        raise ValueError(f"GET /graph-data.js -> {status}")
    text = body.decode("utf-8").strip()
    start = text.index("const GRAPH = ") + len("const GRAPH = ")
    return json.loads(text[start:].rstrip(";"))


def check_graph():
    try:
        graph = load_served_graph()
    except Exception as e:
        report("fail", "Graph data loads with nodes", str(e))
        return None
    nodes, links = graph.get("nodes", []), graph.get("links", [])
    ids = {n.get("id") for n in nodes}
    bad = [l for l in links if l.get("source") not in ids or l.get("target") not in ids]
    if not nodes:
        report("fail", "Graph data loads with nodes", "graph-data.js parsed but has 0 nodes")
        return None
    if bad:
        report("fail", "Graph data loads with nodes", f"{len(bad)} links point at nodes that don't exist")
        return None
    report("pass", "Graph data loads with nodes", f"{len(nodes)} nodes, {len(links)} links")
    return graph


def well_formed(data, node_count):
    """Problems with a /chat reply, or [] if it is well formed."""
    problems = []
    if not isinstance(data.get("answer"), str) or not data["answer"].strip():
        problems.append("no answer text")
    nodes = data.get("nodes")
    if not isinstance(nodes, list):
        problems.append("nodes is not an array")
    elif any(not isinstance(i, int) or not 0 <= i < node_count for i in nodes):
        problems.append(f"nodes has ids outside the graph: {nodes}")
    if "USED:" in str(data.get("answer", "")).upper():
        problems.append("the USED: protocol line leaked into the answer")
    return problems


def check_chat(graph):
    # Pick a real note and ask about its title, so the notes genuinely cover the question.
    target = next((n for n in graph["nodes"] if n.get("group") != "captures"), graph["nodes"][0])
    question = f"What does the {target['label']} note say? One sentence."
    t0 = time.time()
    status, data = post_json("/chat", {"question": question})
    took = time.time() - t0
    if status != 200:
        report("fail", "/chat answers through the Claude subprocess", f"HTTP {status}: {short(data.get('error'))}")
        return
    problems = well_formed(data, len(graph["nodes"]))
    if problems:
        report("fail", "/chat answers through the Claude subprocess", "; ".join(problems))
        return
    nodes = data["nodes"]
    detail = f"{took:.1f}s, nodes {nodes}: \"{short(data['answer'])}\""
    if target["id"] not in nodes:
        report("warn", "/chat answers through the Claude subprocess",
               detail + f"\n          well formed, but it didn't cite the note it was asked about (id {target['id']})")
    else:
        report("pass", "/chat answers through the Claude subprocess", detail)


def check_claude_wrapper():
    """Run the CLI the way server.py does, with every API key stripped, and read how it authenticated."""
    name = "Claude Code CLI answers on the subscription (no API key)"
    try:
        exe = server.find_claude()
    except SystemExit as e:
        report("fail", name, str(e))
        return
    env = os.environ.copy()
    for var in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        env.pop(var, None)
    env.pop("CLAUDECODE", None)
    cmd = [exe, "-p", "--tools", "", "--no-session-persistence", "--strict-mcp-config",
           "--output-format", "stream-json", "--verbose"]
    t0 = time.time()
    try:
        proc = subprocess.run(cmd, input="Reply with exactly: PREFLIGHT_OK", capture_output=True, text=True,
                              encoding="utf-8", errors="replace", env=env, timeout=120,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired:
        report("fail", name, "no answer within 120 s")
        return
    took = time.time() - t0
    init, result = {}, {}
    for line in proc.stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get("subtype") == "init":
            init = event
        if event.get("type") == "result":
            result = event
    key_source = init.get("apiKeySource")
    answer = str(result.get("result", ""))
    if proc.returncode != 0 or result.get("is_error") or "PREFLIGHT_OK" not in answer:
        report("fail", name, f"exit {proc.returncode}: {short(answer or proc.stderr or 'no output')}")
    elif key_source not in (None, "none"):
        report("fail", name, f"it authenticated with an API key ({key_source}), not the subscription")
    else:
        report("pass", name, f"{os.path.basename(exe)} {took:.1f}s, apiKeySource={key_source}, "
                             f"MCP servers loaded: {len(init.get('mcp_servers', []))}")
    if server.load_config().get("auth_type") != "subscription_bridge":
        report("warn", "config.json auth_type", "is not subscription_bridge, so server.py won't strip API keys")
    if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        report("warn", "API key in your environment",
               "ANTHROPIC_API_KEY/AUTH_TOKEN is set; server.py strips it, but it is worth removing")


def check_remember(graph):
    name = "/remember writes a real file that /chat can find"
    captures = os.path.join(server.HOME, "notes", "captures")       # a run cut off mid-way may have left its probe
    for leftover in (os.listdir(captures) if os.path.isdir(captures) else []):
        if leftover.startswith("Preflight Probe Code Is ") and leftover.endswith(".md"):
            os.remove(os.path.join(captures, leftover))
            note(f"removed a leftover probe note from an interrupted run: {leftover}")
    code =f"{random.choice(['Zephyr', 'Marmalade', 'Quokka', 'Tangerine'])} {random.randint(1000, 9999)}"
    status, data = post_json("/remember", {"text": f"remember that the preflight probe code is {code}"})
    if status != 200:
        report("fail", name, f"HTTP {status}: {short(data.get('error'))}")
        return
    node = data.get("node") or {}
    path = os.path.join(server.HOME, *str(node.get("path", "")).split("/"))   # paths are relative to your data folder
    try:
        try:
            with open(path, encoding="utf-8") as f:
                on_disk = f.read()
        except OSError as e:
            report("fail", name, f"server said {node.get('path')!r} but it isn't on disk: {e}")
            return
        if code not in on_disk:
            report("fail", name, f"{node.get('path')} exists but doesn't contain {code!r}")
            return
        served = load_served_graph()
        if not any(n.get("id") == node.get("id") and n.get("label") == node.get("label") for n in served["nodes"]):
            report("fail", name, "the file was written but the served graph-data.js doesn't include it")
            return
        status, reply = post_json("/chat", {"question": "What is the preflight probe code?"})
        if status != 200:
            report("fail", name, f"file written, but /chat failed: HTTP {status} {short(reply.get('error'))}")
            return
        problems = well_formed(reply, len(served["nodes"]))
        digits = code.split()[1]
        if problems or digits not in reply.get("answer", "") or node.get("id") not in reply.get("nodes", []):
            why = "; ".join(problems) or f"answer missed {code!r} or didn't cite node {node.get('id')}"
            report("fail", name, f"{why}: \"{short(reply.get('answer'))}\"")
            return
        report("pass", name, f"{node.get('path')} (node {node.get('id')}), then /chat: \"{short(reply['answer'])}\"")
    finally:
        if os.path.isfile(path) and os.path.basename(os.path.dirname(path)) == "captures":
            os.remove(path)
            note(f"probe note deleted; its star leaves the galaxy at the next server restart")


def check_see():
    name = "/see answers a real JPEG through Claude's vision"
    shape = random.choice(["circle", "square", "triangle"])
    jpeg = encode_jpeg(draw_shape(shape), 320, 240)
    if jpeg[:3] != b"\xff\xd8\xff" or server.jpeg_size(jpeg) != (320, 240):
        report("fail", name, "preflight's own JPEG encoder produced a bad file")
        return
    q = urllib.parse.quote("What single shape is on this screen? One word first.")
    t0 = time.time()
    status, ctype, raw = request("POST", f"/see?q={q}&t={int(time.time() * 1000)}", jpeg,
                                 {"Content-Type": "image/jpeg"})
    took = time.time() - t0
    try:
        data = json.loads(raw)
    except ValueError:
        data = {"error": short(raw)}
    if status != 200:
        report("fail", name, f"HTTP {status}: {short(data.get('error'))}")
        return
    answer = str(data.get("answer", ""))
    words = {"circle": ("circle", "disc", "disk", "round"), "square": ("square",), "triangle": ("triangle",)}
    seen = any(w in answer.lower() for w in words[shape])
    frame = data.get("frame") or {}
    detail = (f"sent a {shape} as image/jpeg ({len(jpeg)} bytes), server read {frame.get('width')}x"
              f"{frame.get('height')}, {took:.1f}s: \"{short(answer)}\"")
    report("pass" if seen else "fail", name, detail if seen else detail + f"\n          it never said {shape}")


def check_attach():
    name = "/attach reads a file from the + button, and refuses one it can't"
    word = random.choice(["MARMALADE", "PARSNIP", "LIGHTHOUSE", "WALRUS"])
    doc = f"Lecture notes.\nThe code word for today is {word}.\n".encode()
    t0 = time.time()
    status, _, raw = request("POST", "/attach", json.dumps({"question": "What is the code word in this file? Just the word.",
        "files": [{"name": "notes.txt", "type": "text/plain", "data": base64.b64encode(doc).decode()}]}).encode(),
        {"Content-Type": "application/json"})
    took = time.time() - t0
    try:
        data = json.loads(raw)
    except ValueError:
        data = {"error": short(raw)}
    answer = str(data.get("answer", ""))
    if status != 200 or word.lower() not in answer.lower():
        report("fail", name, f"HTTP {status}: {short(data.get('error') or answer)}")
        return
    status2, _, raw2 = request("POST", "/attach", json.dumps({"question": "read it",
        "files": [{"name": "essay.docx", "type": "application/octet-stream", "data": base64.b64encode(b"PK\x03\x04").decode()}]}).encode(),
        {"Content-Type": "application/json"})
    refused = status2 == 415
    post_json("/chat", {"reset": True})
    report("pass" if refused else "fail", name,
           f"text file with code word {word} read in {took:.1f}s: \"{short(answer)}\"; .docx "
           + ("refused (415)" if refused else f"NOT refused: HTTP {status2}"))


def check_refusals():
    """/see must turn away anything that isn't a fresh JPEG (no Claude call is made for these)."""
    name = "/see refuses non-JPEG and stale frames"
    jpeg = encode_jpeg([[255] * 16 for _ in range(16)], 16, 16)
    q = urllib.parse.quote("what is this?")
    now = int(time.time() * 1000)
    probes = [("image/png", now, jpeg, 415), ("image/jpeg", now, b"not a jpeg at all", 415),
              ("image/jpeg", now - 120_000, jpeg, 409)]
    wrong = []
    for ctype, t, body, want in probes:
        status, _, _ = request("POST", f"/see?q={q}&t={t}", body, {"Content-Type": ctype}, timeout=20)
        if status != want:
            wrong.append(f"{ctype}{' stale' if t != now else ''} -> {status} (wanted {want})")
    report("fail" if wrong else "pass", name, "; ".join(wrong) or "png 415, fake jpeg 415, 2-minute-old frame 409")


def check_model_switch():
    """Switch to a real model and prove the next answer came from it; refuse one that doesn't exist;
    go back to normal. Whatever brain was active before is put back afterwards."""
    name = "/model switches brains and refuses ones that don't exist"
    status, _, raw = request("GET", "/model", timeout=10)
    if status != 200:
        report("fail", name, f"GET /model -> {status}")
        return
    before = json.loads(raw)["model"]
    problems, notes = [], []
    try:
        status, r = post_json("/model", {"text": "switch to Opus 6"})
        if status != 200 or r.get("ok") is not False or r.get("model", {}).get("id") != before["id"]:
            problems.append(f"Opus 6 was not refused cleanly: HTTP {status} {short(r)}")
        elif "Here I can run" not in r.get("say", "") or "Opus 6" in r.get("supported", []):
            problems.append(f"the Opus 6 refusal didn't list the real models: {short(r.get('say'))}")
        else:
            notes.append(f"Opus 6 refused; supported: {', '.join(r.get('supported', []))}")

        status, r = post_json("/model", {"text": "try on Haiku 5.5"})
        if status != 200 or not r.get("ok") or r.get("model", {}).get("id") != "claude-haiku-5-5":
            problems.append(f"switch to Haiku 5.5 failed: HTTP {status} {short(r.get('say') or r.get('error'))}")
        else:
            status, c = post_json("/chat", {"question": "Say hello in five words."})
            used = (c.get("model") or {}).get("id")
            if status != 200 or used != "claude-haiku-5-5":
                problems.append(f"after switching, /chat was answered by {used} (HTTP {status})")
            else:
                notes.append("Haiku 5.5 answered the next /chat")

        status, r = post_json("/model", {"text": "go back to your normal brain"})
        if status != 200 or not r.get("ok") or not r.get("model", {}).get("default"):
            problems.append(f"'go back to your normal brain' didn't return to the default: {short(r)}")
        else:
            notes.append(f"back to {r['model']['name']} (default)")
    finally:
        if not before.get("default"):
            post_json("/model", {"text": f"switch to {before['name']}"})
    if problems:
        report("fail", name, "; ".join(problems))
    else:
        report("pass", name, "; ".join(notes))


def check_standing():
    """The routing table (every row must pass), and urgent-means-once against a stub clock."""
    import contextlib
    import io
    import shutil
    import tempfile
    import standing
    name = "Standing orders: routing table passes, urgent means once"
    with contextlib.redirect_stdout(io.StringIO()):
        passed, total = standing.run_table()
    problems = [] if passed == total else [f"routing table {passed}/{total}"]
    tmp = tempfile.mkdtemp(prefix="preflight-standing-")
    try:
        clock = [5000.0]
        s = standing.Standing(os.path.join(tmp, "s.json"), clock=lambda: clock[0])
        s.arm("only alert me if it's urgent", "general")
        slots = []
        for _ in range(5):                                # a burst: only the first may go
            ok = s.urgent_slot()
            slots.append(ok)
            if ok:
                s.note_urgent_push()
        clock[0] += standing.URGENT_COOLDOWN
        if slots != [True, False, False, False, False] or not s.urgent_slot():
            problems.append(f"cooldown wrong: burst {slots}, after ten minutes {s.urgent_slot()}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    status, _, raw = request("GET", "/desk", timeout=10)
    armed = (json.loads(raw).get("standing") or {}).get("armed") if status == 200 else None
    report("fail" if problems else "pass", name, "; ".join(problems) or
           f"{passed}/{total} sentences routed right; one urgent push per {standing.URGENT_COOLDOWN // 60} min; "
           f"filter is {'ARMED' if armed else 'off'} right now")


def check_schedules():
    """Reminder and briefing phrasing, and the daily trigger: fires once, catches up, survives restarts."""
    import datetime
    import shutil
    import tempfile
    import briefing
    import desk
    name = "Schedules: reminders and the daily briefing fire once and survive restarts"
    problems = []
    now = datetime.datetime(2026, 1, 5, 14, 30)
    r = desk.parse_reminder("in twenty minutes give me a reminder about the invoice", now)
    if not r or r[0] != "add" or r[2] != now + datetime.timedelta(minutes=20) or r[1] != "The invoice":
        problems.append(f"reminder phrasing parsed as {r}")
    if briefing.parse("give me the briefing every morning at 8") != ("schedule", 8, 0, "daily"):
        problems.append("'every morning at 8' not understood")
    if briefing.parse("give me the briefing now") != ("now",):
        problems.append("'briefing now' not understood")
    tmp = tempfile.mkdtemp(prefix="preflight-sched-")
    try:
        path, clock = os.path.join(tmp, "s.json"), [datetime.datetime(2026, 1, 5, 7, 59)]
        briefing.Briefing(path, None, None, None, clock=lambda: clock[0]).schedule(8, 0, "daily")
        s = briefing.Briefing(path, None, None, None, clock=lambda: clock[0])      # as after a restart
        seq = [s.due()]
        clock[0] = datetime.datetime(2026, 1, 5, 8, 0)
        seq += [s.due(), briefing.Briefing(path, None, None, None, clock=lambda: clock[0]).due()]
        clock[0] = datetime.datetime(2026, 1, 6, 10, 30)                          # off at 8, back at 10:30
        late = briefing.Briefing(path, None, None, None, clock=lambda: clock[0]).due()
        if seq != [False, True, False] or late:
            problems.append(f"daily trigger fired {seq}, late catch-up {late}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    status, _, raw = request("GET", "/desk", timeout=10)
    pending = len(json.loads(raw).get("reminders", [])) if status == 200 else "?"
    report("fail" if problems else "pass", name, "; ".join(problems) or
           f"phrasing understood; 8:00 fires once, not again after a restart, skipped if 2+ hours late; {pending} reminder(s) pending")


def check_hands():
    """The local hands, offline (dry run, nothing opens): apps found, Chrome found, and the limits hold."""
    import shutil
    import tempfile
    import localhand
    name = "Local hands: apps and Chrome found; workspace, link and program limits hold"
    problems, tmp = [], tempfile.mkdtemp(prefix="preflight-hands-")
    try:
        cfg = os.path.join(tmp, "c.json")
        with open(cfg, "w") as f:
            json.dump({"workspace_dirs": [os.path.join(tmp, "ws")]}, f)
        os.makedirs(os.path.join(tmp, "ws", "sub"))
        open(os.path.join(tmp, "ws", "run.bat"), "w").close()
        open(os.path.join(tmp, "ws", "a.txt"), "w").close()
        open(os.path.join(tmp, "ws", "sub", "a.txt"), "w").close()
        h = localhand.Hands(cfg, os.path.join(tmp, "unused"), dry_run=True)
        refused = _refuses
        for label, fn, args in [("a path outside the workspace", h.list_folder, ("C:/Windows",)),
                                ("a ..\\ escape", h.list_folder, ("..\\..",)),
                                ("a javascript: link", h.open_url, ("javascript:alert(1)",)),
                                ("a file: link", h.open_url, ("file:///C:/Windows/win.ini",)),
                                ("opening a .bat", h.open_path, ("run.bat",)),
                                ("overwriting by move", h.move_path, ("a.txt", "sub"))]:
            if not refused(fn, *args):
                problems.append(f"{label} was allowed")
        if h.launched:
            problems.append(f"something launched: {h.launched}")
        apps = h.apps()
        if not apps:
            problems.append("no Start-menu apps found")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    import server
    roots = localhand.Hands(server_config_path(), server.WORKSPACE_ROOT).roots()
    report("fail" if problems else "pass", name, "; ".join(problems) or
           f"{len(apps)} apps, Chrome {'found' if h.chrome() else 'NOT found (default browser used)'}; outside paths, "
           f"..\\ escapes, javascript:/file: links, .bat files and overwrites all refused; workspace: "
           f"{', '.join(roots) or 'Desktop/Jarvis'}")


def _refuses(fn, *args):
    import localhand
    try:
        fn(*args)
        return False
    except localhand.HandError:
        return True


def server_config_path():
    import server
    return server.CONFIG_PATH


def check_post_lockdown():
    """Only the galaxy page may POST: not another website, not a form, not a rebound hostname."""
    name = "Only the page itself can POST (no other site, form or hostname)"
    body = json.dumps({"reset": True}).encode()
    probes = [("another website", {"Content-Type": "application/json", "Origin": "https://evil.example"}),
              ("text/plain form post", {"Content-Type": "text/plain"}),
              ("DNS-rebound Host", {"Content-Type": "application/json", "Host": "evil.example:4700"})]
    leaks = []
    for label, headers in probes:
        conn = http.client.HTTPConnection(HOST, PORT, timeout=10)
        conn.putrequest("POST", "/chat", skip_host="Host" in headers)
        for k, v in {**headers, "Content-Length": str(len(body))}.items():
            conn.putheader(k, v)
        conn.endheaders(body)
        status = conn.getresponse().status
        conn.close()
        if status != 403:
            leaks.append(f"{label} -> {status}")
    report("fail" if leaks else "pass", name, "; ".join(leaks) or "3 cross-site shapes refused with 403")


def check_google():
    """The Google tools: guards always (no network), real read-only calls once connected."""
    import gtools
    name = "Google tools: guards hold, and real reads work once connected"
    problems = []
    st = {"read_outside": True, "refused": [], "receipts": [], "tools": []}
    tc = gtools.ToolChat(None, None, "")
    res = tc.execute({"name": "gcal_move", "args": {"event_id": "x", "new_start": "2026-01-01T10:00"}}, st)
    if not res.get("refused"):
        problems.append("a move after a read was not refused")
    if tc.execute({"name": "gmail_send", "args": {}}, st).get("ok"):
        problems.append("a send tool exists")
    if not tc.check_receipts("I've drafted a reply to Maya.", [])[1]:
        problems.append("an unconfirmed 'I've drafted' claim went uncorrected")
    if 'trust="untrusted"' not in gtools.untrusted("gmail_read", {"body": "x"}):
        problems.append("tool results are not labelled untrusted")
    if problems:
        report("fail", name, "; ".join(problems))
        return
    status, _, raw = request("GET", "/google/status", timeout=30)
    s = json.loads(raw) if status == 200 else {}
    if not s.get("connected"):
        why = ("config.json needs " + ", ".join(s.get("missing", []))) if s.get("missing") else "press Connect Google"
        report("warn", name, f"guards hold (taint refusal, no send, receipts, untrusted labels); not connected: {why}")
        return
    g = gtools.Google(server.CONFIG_PATH, server.HOME, PORT)
    try:
        events = g.gcal_events()
        unread = g.gmail_unread(max_results=1)
        detail = (f"connected as {s.get('email')}: {len(events['events'])} events today, "
                  f"about {unread['estimated_total']} unread (read-only checks)")
        if s.get("missing_scopes"):
            report("warn", name, detail + f"; not yet granted: {', '.join(s['missing_scopes'])} (press Reconnect Google)")
        else:
            report("pass", name, detail)
    except gtools.GoogleError as e:
        report("fail", name, f"connected as {s.get('email')}, but a read failed: {e}")


# ---------------------------------------------------------------- security validation

def check_security():
    """One line per requirement: the browser is not trusted, limits hold, secrets are locked,
    nothing from outside is code, and every CDN script is pinned."""
    import secure
    print("\n  Security validation", flush=True)
    H = f"{HOST}:{PORT}"

    # 1. Foreign Host (DNS rebinding): refused on every kind of request.
    got = {}
    for method, path in (("GET", "/"), ("GET", "/graph-data.js"), ("GET", "/desk"), ("GET", "/model")):
        got[f"{method} {path}"] = request(method, path, headers={"Host": "evil.example:4700"}, timeout=10)[0]
    got["POST /chat"] = request("POST", "/chat", b'{"reset":true}', {"Host": "evil.example:4700",
                                                                     "Content-Type": "application/json"}, timeout=10)[0]
    got["GET / (Host localhost:9999)"] = request("GET", "/", headers={"Host": f"localhost:{PORT + 1}"}, timeout=10)[0]
    bad = {k: v for k, v in got.items() if v != 403}
    report("fail" if bad else "pass", "Security: a foreign Host header is refused",
           f"not refused: {bad}" if bad else f"{len(got)} requests with foreign Hosts all got 403")

    # 2. Another website may only navigate to the app: never load its scripts or data, never frame it.
    xs = lambda mode, dest: {"Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": mode, "Sec-Fetch-Dest": dest}
    cases = [("<script src=…/graph-data.js>", "/graph-data.js", xs("no-cors", "script"), 403),
             ("fetch('/desk') from a website", "/desk", xs("cors", "empty"), 403),
             ("<img src=…/> from a website", "/", xs("no-cors", "image"), 403),
             ("<iframe src=…/> from a website", "/", xs("navigate", "iframe"), 403),
             ("<iframe> even same-origin", "/", {"Sec-Fetch-Site": "same-origin", "Sec-Fetch-Mode": "navigate", "Sec-Fetch-Dest": "iframe"}, 403),
             ("navigate to /graph-data.js from a website", "/graph-data.js", xs("navigate", "document"), 403),
             ("navigate to /oauth/start from a website", "/oauth/start", xs("navigate", "document"), 403),
             ("navigate to the app from a website", "/", xs("navigate", "document"), 200),
             ("the page loading its own script", "/graph-data.js", {"Sec-Fetch-Site": "same-origin", "Sec-Fetch-Mode": "no-cors",
                                                                   "Sec-Fetch-Dest": "script"}, 200)]
    wrong = [f"{label} -> {request('GET', path, headers=h, timeout=10)[0]} (wanted {want})" for label, path, h, want in cases
             if request("GET", path, headers=h, timeout=10)[0] != want]
    report("fail" if wrong else "pass", "Security: a cross-site script load or embed is refused; navigation only",
           "; ".join(wrong) or f"{len(cases)} cases: scripts, data, images and frames 403; opening the app 200")

    # 3. Every POST must carry our own Origin.
    body = b'{"reset": true}'
    jh = {"Content-Type": "application/json"}
    results = {"foreign Origin": request("POST", "/chat", body, {**jh, "Origin": "https://evil.example"}, timeout=10)[0],
               "Origin: null (sandboxed page)": request("POST", "/chat", body, {**jh, "Origin": "null"}, timeout=10)[0],
               "no Origin at all": request("POST", "/chat", body, {**jh, "Origin": None}, timeout=10)[0],
               "own Origin, text/plain (a form)": request("POST", "/chat", body, {"Content-Type": "text/plain"}, timeout=10)[0],
               "own Origin, JSON": request("POST", "/chat", body, jh, timeout=10)[0]}
    want = {"own Origin, JSON": 200, "own Origin, text/plain (a form)": 415}
    bad = {k: v for k, v in results.items() if v != want.get(k, 403)}
    report("fail" if bad else "pass", "Security: a POST without our own Origin is refused",
           f"wrong: {bad}" if bad else "foreign, null and missing Origin 403; a form-type body 415; the page's own POST 200")

    # 4. Bodies are capped and refused cleanly, without being read.
    big = 70 * 1024
    st1, h1, t1 = raw_exchange(f"POST /chat HTTP/1.1\r\nHost: {H}\r\nOrigin: {OWN_ORIGIN}\r\nContent-Type: application/json\r\n"
                               f"Content-Length: {big}\r\n\r\n")
    st2, _, t2 = raw_exchange(f"POST /see?q=x&t=0 HTTP/1.1\r\nHost: {H}\r\nOrigin: {OWN_ORIGIN}\r\nContent-Type: image/jpeg\r\n"
                              f"Content-Length: {50 * 1024 * 1024}\r\n\r\n")
    st3, _, _ = raw_exchange(f"POST /chat HTTP/1.1\r\nHost: {H}\r\nOrigin: {OWN_ORIGIN}\r\nContent-Type: application/json\r\n"
                             f"Content-Length: -5\r\n\r\n")
    st4, _, _ = raw_exchange(f"POST /chat HTTP/1.1\r\nHost: {H}\r\nOrigin: {OWN_ORIGIN}\r\nContent-Type: application/json\r\n"
                             f"Transfer-Encoding: chunked\r\n\r\n5\r\nhello\r\n0\r\n\r\n")
    ok = st1 == 413 and st2 == 413 and st3 == 400 and st4 == 411 and t1 < 3 and t2 < 3
    report("pass" if ok else "fail", "Security: an oversized or malformed body is refused cleanly",
           f"70 KB JSON -> {st1} in {t1:.2f}s (unread); 50 MB image -> {st2} in {t2:.2f}s; Content-Length -5 -> {st3}; "
           f"chunked -> {st4}")

    # 5. Socket timeouts: a client that stalls mid-request is dropped.
    import server
    t0 = time.time()
    with socket.create_connection((HOST, PORT), timeout=server.REQUEST_TIMEOUT + 15) as sock:
        sock.sendall(f"GET / HTTP/1.1\r\nHost: {H}\r\n".encode())          # ...and never finishes the headers
        try:
            closed = sock.recv(1) == b""
        except socket.timeout:
            closed = False
    took = time.time() - t0
    report("pass" if closed and took <= server.REQUEST_TIMEOUT + 5 else "fail", "Security: a stalled client is dropped (socket timeout)",
           f"connection closed by the server after {took:.0f}s (limit {server.REQUEST_TIMEOUT}s)")

    # 6. Only known file types from viewer/, never backups, temp files, dotfiles or folder listings.
    probes = {"index.html.bak": b"x", "index.html~": b"x", "graph-data.js.tmp": b"x", ".env": b"SECRET=1",
              "notes.old.html": b"x", "preflight-probe.txt": b"x"}
    made = []
    try:
        for name, data in probes.items():
            path = os.path.join(VIEWER_DIR, name)
            if not os.path.exists(path):
                with open(path, "wb") as f:
                    f.write(data)
                made.append(path)
        os.makedirs(os.path.join(VIEWER_DIR, "preflight-dir"), exist_ok=True)
        made.append(os.path.join(VIEWER_DIR, "preflight-dir"))
        served = {n: request("GET", "/" + urllib.parse.quote(n), timeout=10)[0] for n in probes}
        served["preflight-dir/ (listing)"] = request("GET", "/preflight-dir/", timeout=10)[0]
        served["HEAD /index.html.bak"] = request("HEAD", "/index.html.bak", timeout=10)[0]
        served["/..%2fserver.py"] = request("GET", "/..%2fserver.py", timeout=10)[0]
    finally:
        for path in reversed(made):
            (os.rmdir if os.path.isdir(path) else os.remove)(path)
    leaks = {k: v for k, v in served.items() if v != 404}
    report("fail" if leaks else "pass", "Security: only known file types are served; no backups or listings",
           f"served: {leaks}" if leaks else f"{len(served)} backup/temp/dot/listing/traversal probes all 404")

    # 7. Response headers: nosniff, same-origin resources, no referrer, never framed.
    need = {"x-content-type-options": "nosniff", "cross-origin-resource-policy": "same-origin",
            "referrer-policy": "no-referrer", "x-frame-options": "DENY"}
    missing = []
    for path in ("/", "/graph-data.js", "/desk", "/nope"):
        _, hdrs, _ = raw_exchange(f"GET {path} HTTP/1.1\r\nHost: {H}\r\nConnection: close\r\n\r\n")
        missing += [f"{path} {k}" for k, v in need.items() if hdrs.get(k) != v]
        if "frame-ancestors 'none'" not in hdrs.get("content-security-policy", ""):
            missing.append(f"{path} frame-ancestors")
    _, hdrs, _ = raw_exchange(f"GET / HTTP/1.1\r\nHost: {H}\r\nConnection: close\r\n\r\n")
    csp = hdrs.get("content-security-policy", "")
    if "'unsafe-inline'" in csp.split("script-src", 1)[-1].split(";")[0] or "'unsafe-eval'" in csp:
        missing.append("page CSP allows inline or eval script")
    report("fail" if missing else "pass", "Security: protective headers on every response",
           "; ".join(missing) or "nosniff, CORP same-origin, no-referrer, X-Frame-Options DENY, frame-ancestors 'none' on pages, "
                                  "data, scripts and errors; page scripts allowed by hash only")

    # 8. Secrets: owner-only (Windows ACL / chmod 600), and covered by .gitignore.
    paths = secure.secret_paths(ROOT)
    loose = [os.path.relpath(p, ROOT) + " " + " ".join(secure.entries(p)) for p in paths if not secure.is_locked(p)]
    report("fail" if loose else "pass", "Security: secret and state files are readable by this account only",
           "; ".join(loose) or f"{len(paths)} paths (config.json, secrets/, data/ and every file in them) owner-only")
    try:
        with open(os.path.join(ROOT, ".gitignore"), encoding="utf-8") as f:
            ignore = f.read().split()
    except OSError:
        ignore = []
    need_ignore = ["config.json*", "secrets/", "data/", "*.dpapi", "*token*", ".env", "config.local.yaml",
                   "personal_strings.local.txt", "profile.yaml", "listings.db"]
    gaps = [x for x in need_ignore if x not in ignore]
    report("fail" if gaps else "pass", "Security: .gitignore covers config and every token/state file",
           f"missing: {gaps}" if gaps else ", ".join(need_ignore))

    # 9. Nothing from outside is code: escaping in the viewer, http(s)-only links, pinned + integrity-checked CDN.
    with open(os.path.join(VIEWER_DIR, "index.html"), encoding="utf-8") as f:
        page = f.read()
    probs = []
    if 'href="${esc(r.link)}' in page or re.search(r'href="\$\{(?!esc\(href\))', page):
        probs.append("a link is built without safeUrl()")
    if "${n.label}" in page or re.search(r"<b[^>]*>\$\{g\}", page):
        probs.append("a graph label or legend entry is not escaped")
    if "'\"': \"&quot;\", \"'\": \"&#39;\"" not in page and "&#39;" not in page:
        probs.append("esc() doesn't escape single quotes")
    if "/^https?:$/" not in page:
        probs.append("safeUrl() doesn't restrict links to http/https")
    scripts = re.findall(r"<script\s+src=\"([^\"]+)\"([^>]*)>", page)
    cdn = [(src, attrs) for src, attrs in scripts if src.startswith(("http://", "https://", "//"))]
    for src, attrs in cdn:
        if not re.search(r"@\d+\.\d+\.\d+/", src):
            probs.append(f"{src} is not pinned to an exact version")
        if 'integrity="sha384-' not in attrs or 'crossorigin="anonymous"' not in attrs:
            probs.append(f"{src} has no integrity hash")
    if re.search(r"<script[^>]+type=\"module\"", page) or re.search(r"\bimport\s*\(", page):
        probs.append("a module or dynamic import could load unpinned code")
    report("fail" if probs else "pass", "Security: outside text renders as text; links http(s) only; CDN pinned",
           "; ".join(probs) or f"esc() on all outside text, safeUrl() on all links, {len(cdn)} CDN scripts pinned to "
                               f"exact versions with sha384 integrity")


def check_served_files():
    name = "Files the browser is served match the files on disk"
    mismatched, checked = [], 0
    for dirpath, _, files in os.walk(VIEWER_DIR):
        for fname in files:
            if fname.endswith(".tmp"):
                continue
            path = os.path.join(dirpath, fname)
            rel = os.path.relpath(path, VIEWER_DIR).replace(os.sep, "/")
            if rel == "graph-data.js":                    # rebuilt at runtime, in your data folder when it's separate
                path = server.build.OUT_FILE
            status, _, body = request("GET", "/" + urllib.parse.quote(rel), timeout=10)
            with open(path, "rb") as f:
                disk = f.read()
            checked += 1
            if status != 200 or hashlib.sha256(body).digest() != hashlib.sha256(disk).digest():
                mismatched.append(f"{rel} (HTTP {status}, served {len(body)} vs disk {len(disk)} bytes)")
    _, _, root = request("GET", "/", timeout=10)
    with open(os.path.join(VIEWER_DIR, "index.html"), "rb") as f:
        if root != f.read():
            mismatched.append("/ is not viewer/index.html")
    if mismatched:
        report("fail", name, "; ".join(mismatched))
    else:
        report("pass", name, f"{checked} files in viewer/ plus /, byte for byte (sha256)")


def check_config_hidden():
    name = "config.json is not reachable from the browser"
    with open(server.CONFIG_PATH, "rb") as f:
        secret = f.read().strip()
    paths = ["/config.json", "/../config.json", "/%2e%2e/config.json", "/..%2fconfig.json",
             "/%2e%2e%2fconfig.json", "/..%5cconfig.json", "/viewer/../config.json", "//config.json",
             "/./config.json", "/graph-data.js/../../config.json"]
    leaks = []
    for p in paths:
        try:
            status, _, body = request("GET", p, timeout=10)
        except OSError as e:
            continue
        if status == 200 or (secret and secret in body) or b"auth_type" in body:
            leaks.append(f"{p} -> {status}")
    if leaks:
        report("fail", name, "reachable: " + "; ".join(leaks))
    else:
        report("pass", name, f"{len(paths)} paths tried, including ../ and encoded traversal: none served it")


def check_memory_and_build():
    """Memory: the panel's add/delete round trip and "what do you know about me" (no model call).
    Self-build: status is served, and the routing sends build commands to it, never to the chat brain."""
    name = "Memory and self-build: panel round trip, voice commands routed"
    problems = []
    status, _, raw = request("GET", "/memory", timeout=10)
    state = json.loads(raw) if status == 200 else {}
    if not {"enabled", "items", "kinds"} <= set(state):
        problems.append(f"GET /memory -> {status}")
    probe = f"preflight probe {random.randint(1000, 9999)}, delete me"
    _, added = post_json("/memory", {"op": "add", "kind": "about", "text": probe})
    item = next((m for m in added.get("items", []) if m.get("text") == probe), None)
    if not item:
        problems.append("adding a memory from the panel failed")
    else:
        _, after = post_json("/memory", {"op": "delete", "id": item["id"]})
        if any(m.get("text") == probe for m in after.get("items", [])):
            problems.append("deleting a memory from the panel failed")
    _, routed = post_json("/route", {"text": "What do you know about me?"})
    if routed.get("route") != "memory" or not routed.get("say"):
        problems.append(f"'what do you know about me' routed to {routed.get('route')}")
    _, routed = post_json("/route", {"text": "How's the build going?"})
    if routed.get("route") != "build":
        problems.append(f"'how's the build going' routed to {routed.get('route')}")
    status, _, raw = request("GET", "/build", timeout=10)
    build = json.loads(raw) if status == 200 else {}
    if "available" not in build:
        problems.append(f"GET /build -> {status}")
    _, stale = post_json("/build", {"op": "apply", "id": "not-a-build"})
    if stale.get("ok") is not False:
        problems.append("a stale Apply button was not refused")
    report("fail" if problems else "pass", name, "; ".join(problems) or
           f"{len(state.get('items', []))} memories, learning {'on' if state.get('enabled') else 'OFF'}; "
           f"self-build {'available' if build.get('available') else 'unavailable (no source_dir)'}"
           + (f", last build {build['status']}" if build.get("status") else ""))


def check_personal_data():
    """scripts/check_no_personal_data.py over every file git would commit (needs no server)."""
    name = "Git: no personal data or secrets in committed files"
    sys.path.insert(0, os.path.join(ROOT, "scripts"))
    try:
        import check_no_personal_data as scanner
        hits, n_files, n_strings = scanner.scan()
    except Exception as e:                                # not a git repo yet, or git missing
        report("warn", name, f"not run: {e}")
        return
    if hits:
        report("fail", name, "; ".join(f"{rel}:{n} {kind}" for rel, n, kind, _ in hits[:8])
               + (f" (+{len(hits) - 8} more)" if len(hits) > 8 else ""))
    elif n_strings is None:
        report("warn", name, f"{n_files} files clean of generic patterns; create personal_strings.local.txt "
                             "so your own name, email and so on are checked too")
    else:
        report("pass", name, f"{n_files} tracked files: none of your {n_strings} personal strings, no emails, "
                             "phone numbers, keys or home-folder paths")


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    print(f"Preflight: live checks against http://{HOST}:{PORT}\n", flush=True)
    t0 = time.time()
    if check_server():
        graph = check_graph()
        if graph:
            check_chat(graph)
        else:
            report("fail", "/chat answers through the Claude subprocess", "not run: no graph")
        check_claude_wrapper()
        if graph:
            check_remember(graph)
        else:
            report("fail", "/remember writes a real file that /chat can find", "not run: no graph")
        check_see()
        check_attach()
        check_refusals()
        check_model_switch()
        check_google()
        check_standing()
        check_schedules()
        check_hands()
        check_post_lockdown()
        check_served_files()
        check_security()
        check_config_hidden()
        check_memory_and_build()
        check_personal_data()
        try:
            post_json("/chat", {"reset": True})
            note("chat history reset, so the probe questions can't leak into your follow-ups")
        except OSError:
            pass
    else:
        for skipped in ("Graph data loads with nodes", "/chat answers through the Claude subprocess",
                        "/remember writes a real file that /chat can find",
                        "/see answers a real JPEG through Claude's vision",
                        "/attach reads a file from the + button, and refuses one it can't",
                        "/model switches brains and refuses ones that don't exist",
                        "Google tools: guards hold, and real reads work once connected",
                        "Standing orders: routing table passes, urgent means once",
                        "Schedules: reminders and the daily briefing fire once and survive restarts",
                        "Local hands: apps and Chrome found; workspace, link and program limits hold",
                        "Only the page itself can POST (no other site, form or hostname)",
                        "Files the browser is served match the files on disk",
                        "config.json is not reachable from the browser",
                        "Memory and self-build: panel round trip, voice commands routed"):
            report("fail", skipped, "not run: server is down")
        check_claude_wrapper()
        check_personal_data()

    counts = {s: sum(1 for r in results if r[0] == s) for s in ("pass", "fail", "warn")}
    print(f"\n{counts['pass']} pass, {counts['fail']} fail, {counts['warn']} warn  ({time.time() - t0:.0f}s)")
    sys.exit(1 if counts["fail"] else 0)


if __name__ == "__main__":
    main()
