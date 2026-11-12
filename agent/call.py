"""Place one phone call from a GitHub Actions runner and write the transcript back.

    python agent/call.py calls/<id>.json            # dry run: a simulated counterpart, no Twilio
    python agent/call.py calls/<id>.json --live     # real call, needs the Twilio secrets

The runner hosts a tiny webhook server. In live mode a cloudflared quick tunnel
gives it a public https URL; Twilio dials the number and drives the conversation
turn by turn: <Gather input="speech"> hears the other party, the Brain writes the
next line, <Say> speaks it. Every turn is printed as it happens, so the Actions
log is the live transcript. When the call ends, calls/<id>/transcript.md and
result.json are written for the workflow to commit.
"""
import argparse
import base64
import hashlib
import hmac
import json
import os
import re
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from xml.sax.saxutils import escape

sys.path.insert(0, str(Path(__file__).parent))
from brain import Brain, strip_marker  # noqa: E402

PORT = int(os.environ.get("PORT", "8080"))

# A non-UTF-8 console (Windows cp1252) would make the first Chinese line raise
# inside a webhook thread and drop the connection mid-call. Never let that happen.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# Twilio <Say> voices that fit each <Gather> speech language. Override with "voice" in the brief.
DEFAULT_VOICE = {
    "en-US": "Polly.Joanna-Neural", "en-GB": "Polly.Amy-Neural", "en-AU": "Polly.Olivia-Neural",
    "zh-CN": "Polly.Zhiyu-Neural", "zh-TW": "Google.cmn-TW-Standard-A", "zh-HK": "Google.yue-HK-Standard-A",
    "fr-CA": "Polly.Gabrielle-Neural", "fr-FR": "Polly.Lea-Neural", "es-US": "Polly.Lupe-Neural",
    "ja-JP": "Polly.Takumi-Neural", "ko-KR": "Polly.Seoyeon-Neural",
}
TERMINAL = ("completed", "busy", "no-answer", "failed", "canceled")


class Call:
    """One call's state, shared between the webhook threads and the main thread."""

    def __init__(self, brief, live):
        self.brief = brief
        self.live = live
        self.brain = Brain(brief)
        self.transcript = []          # [("bot"|"them", text)]
        self.started = time.time()
        self.ended = threading.Event()
        self.status = "pending"
        self.sid = None
        self.duration = None
        self.empty_turns = 0
        self.lock = threading.Lock()
        self.language = brief.get("language", "en-US")
        self.voice = brief.get("voice") or DEFAULT_VOICE.get(self.language, "Polly.Joanna-Neural")
        self.max_turns = int(brief.get("max_turns", 30))

    def log(self, who, text):
        stamp = f"{time.time() - self.started:6.1f}s"
        print(f"[{stamp}] {'BOT ' if who == 'bot' else 'THEM'}: {text}", flush=True)

    # --- TwiML -------------------------------------------------------------
    def say(self, text):
        return f'<Say voice="{self.voice}" language="{self.language}">{escape(text)}</Say>'

    def gather(self, inner):
        return (f'<Gather input="speech" action="/turn" method="POST" language="{self.language}" '
                f'speechTimeout="auto" actionOnEmptyResult="true">{inner}</Gather>')

    @staticmethod
    def twiml(body):
        return '<?xml version="1.0" encoding="UTF-8"?><Response>' + body + "</Response>"

    def _speak(self, line, done):
        self.transcript.append(("bot", line))
        self.log("bot", line)
        if done:
            return self.twiml(self.say(line) + "<Hangup/>")
        return self.twiml(self.gather(self.say(line)))

    def first_turn(self):
        with self.lock:
            line, done = strip_marker(self._think())
            return self._speak(line, done)

    def _think(self):
        try:
            return self.brain.reply(self.transcript)
        except Exception as e:  # a model error must not strand a live call
            print(f"brain error: {e}", flush=True)
            return self.brief.get("goodbye", "I am sorry, something went wrong on my side. I will call back. Goodbye.") + " <END_CALL>"

    def next_turn(self, heard):
        with self.lock:
            if not heard:
                self.empty_turns += 1
                if self.empty_turns >= 3:
                    return self._speak(self.brief.get("goodbye", "I could not hear you. I will try again later, goodbye."), True)
                return self._speak(self.brief.get("reprompt", "Sorry, I did not catch that. Could you say it again?"), False)
            self.empty_turns = 0
            self.transcript.append(("them", heard))
            self.log("them", heard)
            bot_turns = sum(1 for w, _ in self.transcript if w == "bot")
            line, done = strip_marker(self._think())
            if bot_turns + 1 >= self.max_turns and not done:
                line += " " + self.brief.get("goodbye", "I have taken enough of your time, thank you and goodbye.")
                done = True
            return self._speak(line, done)


# --- webhook server ---------------------------------------------------------
class Handler(BaseHTTPRequestHandler):
    call: Call = None
    public_url: str = ""
    auth_token: str = ""

    def log_message(self, *a):  # keep the Actions log to the transcript
        pass

    def _form(self):
        n = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(n).decode() if n else ""
        return {k: v[0] for k, v in urllib.parse.parse_qs(raw, keep_blank_values=True).items()}

    def _twilio_signed(self, params):
        """Twilio signs every webhook: base64(HMAC-SHA1(token, url + sorted k+v)).

        The tunnel URL is public, so without this anyone who found it could
        inject turns into a live call. In a dry run there is no token and the
        simulator posts straight to localhost.
        """
        if not self.auth_token:
            return True
        url = self.public_url + self.path
        payload = url + "".join(k + params[k] for k in sorted(params))
        want = base64.b64encode(hmac.new(self.auth_token.encode(), payload.encode(), hashlib.sha1).digest()).decode()
        got = self.headers.get("X-Twilio-Signature", "")
        return hmac.compare_digest(want, got)

    def _reply(self, code, body, ctype="text/xml"):
        data = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path == "/health":
            return self._reply(200, "ok", "text/plain")
        self._reply(404, "not found", "text/plain")

    def do_POST(self):
        params = self._form()
        if not self._twilio_signed(params):
            return self._reply(403, "bad signature", "text/plain")
        path = self.path.split("?")[0]
        if path == "/voice":
            return self._reply(200, self.call.first_turn())
        if path == "/turn":
            return self._reply(200, self.call.next_turn(params.get("SpeechResult", "").strip()))
        if path == "/status":
            st = params.get("CallStatus", "")
            print(f"[status] {st}", flush=True)
            if st in TERMINAL:
                self.call.status = st
                if params.get("CallDuration"):
                    self.call.duration = int(params["CallDuration"])
                self.call.ended.set()
            return self._reply(200, "")
        self._reply(404, "not found", "text/plain")


def serve(call):
    Handler.call = call
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


# --- cloudflared quick tunnel ---------------------------------------------
def open_tunnel():
    proc = subprocess.Popen(
        ["cloudflared", "tunnel", "--url", f"http://localhost:{PORT}", "--no-autoupdate"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    url = None
    deadline = time.time() + 60
    while time.time() < deadline and url is None:
        line = proc.stdout.readline()
        if not line:
            break
        m = re.search(r"https://[a-z0-9-]+\.trycloudflare\.com", line)
        if m:
            url = m.group(0)
    if not url:
        proc.kill()
        raise SystemExit("cloudflared did not hand out a trycloudflare.com URL within 60 s")
    # keep draining its output so the process never blocks on a full pipe
    threading.Thread(target=lambda: [None for _ in iter(proc.stdout.readline, "")], daemon=True).start()
    # the edge needs a few seconds before the hostname resolves everywhere
    for _ in range(30):
        try:
            with urllib.request.urlopen(url + "/health", timeout=5) as r:
                if r.status == 200:
                    return proc, url
        except Exception:
            pass
        time.sleep(2)
    proc.kill()
    raise SystemExit(f"tunnel {url} never answered /health")


# --- Twilio REST -------------------------------------------------------------
def twilio(method, path, sid, token, data=None):
    url = f"https://api.twilio.com/2010-04-01/Accounts/{sid}{path}"
    body = urllib.parse.urlencode(data).encode() if data else None
    req = urllib.request.Request(url, data=body, method=method)
    req.add_header("Authorization", "Basic " + base64.b64encode(f"{sid}:{token}".encode()).decode())
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.load(r)


def live_call(call, public_url):
    sid, token, frm = (os.environ.get(k, "") for k in ("TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN", "TWILIO_PHONE_NUMBER"))
    if not (sid and token and frm):
        raise SystemExit("live mode needs TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN and TWILIO_PHONE_NUMBER")
    limit = int(float(call.brief.get("max_minutes", 5)) * 60)
    res = twilio("POST", "/Calls.json", sid, token, {
        "To": call.brief["to"], "From": call.brief.get("from", frm),
        "Url": public_url + "/voice", "Method": "POST",
        "StatusCallback": public_url + "/status", "StatusCallbackMethod": "POST",
        "StatusCallbackEvent": "completed",
        "Timeout": str(int(call.brief.get("ring_seconds", 30))),
        "TimeLimit": str(limit),
    })
    call.sid = res["sid"]
    print(f"[twilio] call {call.sid} -> {call.brief['to']} (status {res['status']})", flush=True)
    # the status callback normally ends the wait; polling covers a lost webhook
    deadline = time.time() + limit + 120
    while not call.ended.wait(5) and time.time() < deadline:
        info = twilio("GET", f"/Calls/{call.sid}.json", sid, token)
        if info["status"] in TERMINAL:
            call.status = info["status"]
            call.duration = int(info.get("duration") or 0)
            call.ended.set()
    if not call.ended.is_set():
        call.status = "timeout"
        try:
            twilio("POST", f"/Calls/{call.sid}.json", sid, token, {"Status": "completed"})
        except Exception as e:
            print(f"hangup failed: {e}", flush=True)


# --- dry run: a simulated counterpart drives the same webhooks ------------------
def simulate(call):
    sim = call.brief.get("simulate", {})
    lines = list(sim.get("script") or [
        "Hello, how can I help you?",
        "Sure, let me check. Yes, that works.",
        "That's right.",
        "You're welcome, goodbye.",
    ])
    base = f"http://127.0.0.1:{PORT}"

    def post(path, form):
        req = urllib.request.Request(base + path, data=urllib.parse.urlencode(form).encode(), method="POST")
        with urllib.request.urlopen(req, timeout=120) as r:
            return r.read().decode()

    def parse(xml):
        root = ET.fromstring(xml)
        return root.find("Gather") is not None, root.find("Hangup") is not None

    expects, hangup = parse(post("/voice", {"CallSid": "SIMULATED", "CallStatus": "in-progress"}))
    turns = 0
    while expects and not hangup and turns < call.max_turns + 2:
        heard = lines.pop(0) if lines else "Okay, thank you, bye."
        expects, hangup = parse(post("/turn", {"CallSid": "SIMULATED", "SpeechResult": heard, "Confidence": "0.9"}))
        turns += 1
    call.status = "completed" if hangup else "simulator-ended"
    call.duration = int(time.time() - call.started)
    call.ended.set()


# --- output -----------------------------------------------------------------------
def write_outputs(call, brief_path):
    out = brief_path.parent / brief_path.stem
    out.mkdir(parents=True, exist_ok=True)
    try:
        summary = call.brain.summarize(call.transcript, call.status)
    except Exception as e:
        summary = f"(summary failed: {e})"
    when = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    md = [f"# Call: {brief_path.stem}", "",
          f"- **To:** {call.brief.get('to_name', '')} {call.brief['to'] if call.live else '(dry run, no number dialled)'}",
          f"- **Goal:** {call.brief['goal']}",
          f"- **When:** {when}",
          f"- **Mode:** {'live' if call.live else 'dry run'} · brain: {call.brain.provider}/{call.brain.model}",
          f"- **Status:** {call.status}" + (f", {call.duration}s" if call.duration is not None else ""),
          "", "## Summary", "", summary, "", "## Transcript", ""]
    md += [f"**{'Bot' if w == 'bot' else 'Them'}:** {t}  " for w, t in call.transcript]
    (out / "transcript.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    (out / "result.json").write_text(json.dumps({
        "id": brief_path.stem, "live": call.live, "status": call.status, "duration_s": call.duration,
        "call_sid": call.sid, "provider": call.brain.provider, "model": call.brain.model,
        "turns": len(call.transcript), "summary": summary, "finished_at": when,
        "transcript": [{"who": w, "text": t} for w, t in call.transcript],
    }, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nwrote {out / 'transcript.md'} and result.json", flush=True)
    step = os.environ.get("GITHUB_STEP_SUMMARY")
    if step:
        with open(step, "a", encoding="utf-8") as f:
            f.write("\n".join(md) + "\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("brief")
    ap.add_argument("--live", action="store_true", help="really dial the number (needs Twilio secrets)")
    a = ap.parse_args()
    brief_path = Path(a.brief)
    brief = json.loads(brief_path.read_text(encoding="utf-8"))
    for k in ("to", "goal", "on_behalf_of"):
        if k not in brief:
            raise SystemExit(f"brief is missing '{k}'")
    live = a.live and bool(brief.get("live"))
    if a.live and not brief.get("live"):
        print('--live given but the brief has no "live": true; running a dry run instead', flush=True)

    call = Call(brief, live)
    print(f"mode: {'LIVE' if live else 'dry run'} · brain: {call.brain.provider}/{call.brain.model} · "
          f"language {call.language} · voice {call.voice}", flush=True)
    srv = serve(call)
    if live:
        tunnel, public_url = open_tunnel()
        Handler.public_url = public_url
        Handler.auth_token = os.environ.get("TWILIO_AUTH_TOKEN", "")
        print(f"[tunnel] {public_url}", flush=True)
        try:
            live_call(call, public_url)
        finally:
            tunnel.kill()
    else:
        simulate(call)
    srv.shutdown()
    write_outputs(call, brief_path)
    print(f"done: {call.status}", flush=True)


if __name__ == "__main__":
    main()
