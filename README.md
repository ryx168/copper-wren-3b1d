# Phone calls from GitHub Actions

An AI assistant that phones a business on your behalf, the way
[Grok Bot's calling feature](https://x.com/bot/status/2118043939592942052) does:
you say who to call and why, it holds the conversation, you get the transcript.
Nothing runs on a PC. A GitHub-hosted runner is the phone.

```
calls/<id>.json  (the brief)            push / dispatch
        │                                      │
        ▼                                      ▼
  GitHub Actions runner ──── cloudflared quick tunnel ──── Twilio dials the number
        │  webhook server                                        │
        │  /voice  first line                                    │ <Say> speaks it
        │  /turn   hears SpeechResult, asks the brain ◄──────────┤ <Gather input="speech">
        │  /status call ended                                    │
        ▼
calls/<id>/transcript.md + result.json   committed back to main
```

## Give it a call

1. Write a brief, `calls/<id>.json` (copy `calls/example.json`):

   | field | meaning |
   |---|---|
   | `to` | E.164 number to dial |
   | `to_name`, `on_behalf_of`, `caller_name` | who is called, for whom, how the bot introduces itself |
   | `goal` | what the call should achieve, in one or two sentences |
   | `facts` | the only things the bot is allowed to state; it never invents a name, date or price |
   | `language` | `en-US`, `zh-CN`, `zh-HK`, `zh-TW`, `fr-CA`, ... (Twilio speech-recognition code); `voice` overrides the matching `<Say>` voice |
   | `disclose_ai` | `true` (default): the first line says it is an AI assistant calling on behalf of you |
   | `max_minutes`, `max_turns` | hard stops; Twilio's `TimeLimit` ends the call at `max_minutes` whatever the model does |
   | `live` | `false` = dry run against a simulated counterpart (`simulate.script`); `true` = really dial |

2. Push it. The workflow runs the brief, prints every turn to the job log as it
   happens (that is the live transcript), and commits `calls/<id>/transcript.md`
   and `result.json` back. A brief that already has a result is not re-run on
   push; re-run it from the Actions tab (`workflow_dispatch`, brief id).

3. Read the summary at the top of `transcript.md`: outcome, confirmed facts, follow-up.

## Secrets

The dry run needs **no secrets**: with no model key it falls back to a scripted
responder, so the pipeline proves itself green on a fresh fork.

| secret | needed for |
|---|---|
| `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_PHONE_NUMBER` | live calls (`"live": true`); the number must be voice-capable and the account's voice geo-permissions must include the destination country |
| `ANTHROPIC_API_KEY` | the brain: Claude (`claude-opus-5` at low effort; set `"model"` in the brief to change it) |
| `XAI_API_KEY` | used instead when no Anthropic key is set: Grok, the model behind Grok Bot |

Only the three Twilio values turn a run into a real phone call, and only for a
brief that says `"live": true`. Forks and pull requests never see the secrets.

## What it does on a live call, and what it does not

- Every webhook is checked against Twilio's `X-Twilio-Signature`, so nobody who
  stumbles on the tunnel URL can inject a turn.
- The opening line discloses that an AI assistant is calling (`disclose_ai`).
  Keep it on; AI-voice calls to people who have not consented are regulated.
- If the other party asks something outside `facts`, the bot says it will check
  and call back. It does not guess.
- Three empty turns in a row (nothing heard) end the call politely.
- Calls are not recorded. The transcript comes from Twilio's speech recognition of
  the other party plus the bot's own lines.
- Outbound only. Nobody can call the runner back.

## Layout

| path | what |
|---|---|
| `agent/call.py` | webhook server, cloudflared tunnel, Twilio REST, the dry-run simulator, output files |
| `agent/brain.py` | the system prompt and the model chain (Claude → Grok → scripted) |
| `calls/*.json` | briefs; `calls/<id>/` holds that call's transcript and result |
| `.github/workflows/call.yml` | the runner job |

## Run it on a PC instead

```
pip install anthropic
python agent/call.py calls/example.json          # dry run
python agent/call.py calls/example.json --live   # needs cloudflared on PATH and the Twilio variables
```
