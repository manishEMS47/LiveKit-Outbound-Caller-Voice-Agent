# LiveKit Outbound Caller Voice Agent

Build and run a voice agent that makes outbound PSTN calls using LiveKit. This example wires up VAD, STT, LLM, and TTS into a Voice Pipeline and includes a small CLI for local development, health checks, and model prewarming.

## Features

- Voice Pipeline with Silero VAD, Deepgram STT, OpenAI LLM + TTS
- Outbound PSTN calls via LiveKit SIP Outbound Trunks
- Callable tools for call control and basic appointment flows
- Simple CLI: `dev`, `healthcheck`, `download-files`, `prewarm`

## Prerequisites

- Python 3.10+
- LiveKit Cloud project and `lk` CLI installed
- Accounts/API keys for OpenAI and Deepgram

## Setup

Run the following commands to clone, create a virtual environment, and install dependencies.

### Linux/macOS

```console
git clone https://github.com/tetratensor/LiveKit-Outbound-Caller-Voice-Agent.git
cd LiveKit-Outbound-Caller-Voice-Agent
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

<details>
  <summary>Windows instructions (click to expand)</summary>
  
```cmd
:: Windows (CMD/PowerShell)
git clone https://github.com/tetratensor/LiveKit-Outbound-Caller-Voice-Agent.git
cd LiveKit-Outbound-Caller-Voice-Agent
python -m venv venv
venv\Scripts\activate
pip install -r requirements.txt
```
</details>

### Configure environment

Copy `.env.example` to `.env.local` and fill in the values:

- `LIVEKIT_URL`
- `LIVEKIT_API_KEY`
- `LIVEKIT_API_SECRET`
- `OPENAI_API_KEY`
- `DEEPGRAM_API_KEY`
- `SIP_OUTBOUND_TRUNK_ID` (from steps below)

You can also populate LiveKit variables via CLI:

```console
lk app env
```

### Optional: pre-download models

Downloads/caches VAD model to avoid a cold start on first run:

```console
python3 agent.py download-files
```

### Validate configuration

```console
python3 agent.py healthcheck
```

### Run the worker (development)

```console
python3 agent.py dev
```

Now the worker is running and waiting for dispatches to make outbound calls.

## Create a Twilio SIP Outbound Trunk

1. Create a Twilio account
2. Get a Twilio phone number
3. Create a SIP trunk
   - Twilio Console → Explore products → Elastic SIP Trunking → SIP Trunks → Get started → Create a SIP Trunk
4. Configure SIP Termination
   - Termination → enter a Termination SIP URI
   - Create Credentials List (friendly name, username, password)

## Create a LiveKit SIP Outbound Trunk

1. Copy `outbound-trunk-example.json` to `outbound-trunk.json` and update with your SIP provider credentials. Do not commit this file.
   - `name`: Any friendly name
   - `address`: Your provider's Termination SIP URI
   - `numbers`: Your Twilio phone number to call from
   - `auth_username`: Username from your credentials list
   - `auth_password`: Password from your credentials list
2. Create the trunk with the CLI:

```console
lk sip outbound create outbound-trunk.json
```

3. Copy the `SIPTrunkID` from the response into `.env.local` as `SIP_OUTBOUND_TRUNK_ID`.

## Make a call

With the worker running in a terminal, open another terminal and dispatch an agent to dial a number:

```console
lk dispatch create \
  --new-room \
  --agent-name outbound-caller \
  --metadata "+1234567890"
```

## Helpful commands

```console
lk project list
```

```console
lk sip outbound list
```

```console
lk sip dispatch list
```

## Troubleshooting

- Ensure all required environment variables are present: run `python3 agent.py healthcheck`.
- First run may download models; use `python3 agent.py download-files` to prewarm.
- For more logs, run with `--log-level DEBUG`, e.g. `python3 agent.py --log-level DEBUG dev`.

## CLI reference

```console
python3 agent.py dev            # run the worker locally
python3 agent.py healthcheck    # validate required env vars
python3 agent.py download-files # pre-download/cache models (e.g., VAD)
python3 agent.py prewarm        # alias to download/cache models
```
