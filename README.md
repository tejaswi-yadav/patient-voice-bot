# Patient Voice Bot

Outbound caller that dials the Athena test line (`+1-805-439-8008`), plays a patient persona over the phone, records the call, and writes up what went wrong.

Built for the Pretty Good AI take-home. Twilio handles the phone leg, OpenAI Realtime plays the patient, Whisper + a small GPT pass handle transcripts and bug notes afterward.

## Setup

```bash
python -m venv venv
.\venv\Scripts\activate          # Windows
pip install -r requirements.txt
cp .env.example .env              # fill in Twilio, OpenAI, ngrok domain
```

You need a Twilio number, an OpenAI key with Realtime access, and ngrok pointing at port 6060:

```bash
ngrok http 6060
```

Put the ngrok host (no `https://`) in `DOMAIN`.

## Run it

Terminal 1 — server (leave it up):

```bash
python run.py serve
```

Terminal 2 — one call or a batch:

```bash
python run.py call --scenario schedule-routine --wait
python run.py batch --wait          # 10 scenarios, waits between calls
python run.py verify                # quick sanity check on calls/
```

Other useful commands: `list`, `retranscribe`, `download-recordings` (if Twilio has recordings you don't have locally).

## Scenarios

`python run.py list` shows all 12. Default batch covers scheduling, cancel/reschedule, refill, hours, insurance, urgent symptoms, and a few edge cases (vague caller, barge-in, Sunday appointment).

## Where files go

```
calls/
  recordings/     recording-{id}.mp3
  transcripts/    transcript-{id}.txt / .json
  metadata/       per-call analysis json
  BUG_REPORT.md   appended after each call
```

Patient lines in the final transcript come from Realtime events during the call. Agent side is mostly from the dual-channel recording via Whisper (patient TTS is quiet on the recording).

## Stuff that broke while building this

- OpenAI's `session.update` shape changed — needs `type: "realtime"` and `audio.input.turn_detection`, not the older flat fields. No `temperature` in GA.
- If VAD has `create_response: true`, don't also send `response.create` on every `speech_stopped` — you get `conversation_already_has_active_response`.
- First calls had one-sided transcripts because Whisper was overwriting realtime patient text; merge fix is in `whisper_transcribe.py`.

## Rough cost

~$10–15 for 10 calls (Twilio minutes + Realtime + cheap GPT analysis).

## Env vars

See `.env.example`. Only `+18054398008` can be dialed — enforced in code.
