# Architecture

Twilio places the outbound call and streams both sides of the audio to a FastAPI WebSocket (`/media-stream`). We forward the agent's μ-law packets straight into OpenAI Realtime and stream the model's audio back to Twilio — no separate STT/LLM/TTS chain. Patient behavior is just system instructions plus a scenario (name, backstory, goals). The bot waits for the agent greeting before talking; server VAD with `create_response` handles turn-taking when the remote side stops speaking.

After hangup, Twilio posts the dual-channel MP3. Whisper transcribes each channel (agent vs patient on the wire), we merge that with patient lines captured live from Realtime, run a short GPT review, and append anything suspicious to `BUG_REPORT.md`. Calls are capped at 3 minutes and can only go to the assessment number.

I picked Realtime over a pipeline because phone conversations need fast back-and-forth — the challenge literally says they listen to the calls first. Scenarios are data, not scripts: the patient reacts to whatever the agent actually says, which is better for finding real bugs than hard-coded dialog. Transcripts are messy to get right on a phone bridge (quiet patient channel, Spanish hold message on the agent side), so realtime patient text is treated as source of truth and the recording fills in the agent.
