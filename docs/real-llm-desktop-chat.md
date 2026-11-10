# Real LLM desktop chat

SOFIA starts in **Offline Mock**. The desktop app can also stream text from
OpenAI or Anthropic Claude after you enable paid sends for the current session.
The CLI and the Agent Loop demonstrations continue to run offline.

## Windows setup

Install the GUI and the optional SDK for the service you plan to use. Install
`.[ui,openai,anthropic]` to make both available.

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[ui,openai]"
# For Anthropic, install ".[ui,anthropic]" instead.
```

Use a trusted secret manager to provide `OPENAI_API_KEY` or `ANTHROPIC_API_KEY`
to the process that launches SOFIA. For a one-time Windows PowerShell session,
read the key with a masked prompt so the key stays out of command history:

```powershell
$sofiaCredentialName = 'OPENAI_API_KEY' # or 'ANTHROPIC_API_KEY'
$sofiaSecret = Read-Host 'Provider API key' -AsSecureString
[Environment]::SetEnvironmentVariable(
    $sofiaCredentialName,
    [pscredential]::new('sofia', $sofiaSecret).GetNetworkCredential().Password,
    'Process'
)
$sofiaSecret.Dispose()
try {
    .\.venv\Scripts\python.exe -m sofia.ui
} finally {
    [Environment]::SetEnvironmentVariable($sofiaCredentialName, $null, 'Process')
}
```

Leave `SOFIA_PROVIDER=mock` for application startup. In **Settings → Conversation
API**, select a provider and its exact model. The credential field shows
**Configured** or **Missing**. It checks presence locally without testing the key.
Review the disclosure and limits, check **Enable paid sends for this session**,
and click **Save**. A subsequent **Send** starts one request. Settings, New Chat,
and startup do not contact either service. Changes to provider, model, or limits
require checking the consent box again. The API controls are locked while a paid
request finishes. Other preferences, including language, can still be saved.

## Models and cost

These first-party standard API rates and limits were checked on November 10, 2026.
Prices are USD per million tokens.

| Provider and exact model | Input / output | Cached input | Context window / model output limit |
| --- | ---: | ---: | ---: |
| OpenAI `gpt-4.1-mini-2025-04-14` | $0.40 / $1.60 | $0.10 | 1,047,576 / 32,768 |
| Anthropic `claude-haiku-4-5-20251001` | $1 / $5 | $0.10 | 200,000 / 64,000 |

Claude's 5-minute and 1-hour cache write rates are $1.25 and $2. SOFIA does
not enable caching for Claude and reserves at the higher write rate to cover
all supported input categories. The OpenAI snapshot is deprecated and scheduled
for shutdown on January 14, 2027. Its replacement requires review of the model
ID, stream events and billing rules before SOFIA can offer it.
[OpenAI model](https://developers.openai.com/api/docs/models/gpt-4.1-mini),
[OpenAI shutdown schedule](https://developers.openai.com/api/docs/deprecations),
[Claude model and pricing](https://platform.claude.com/docs/en/models/haiku-4-5/overview).

Each Send allows one attempt, with a configurable output cap of 16–2,048 tokens
and a $0.01–$1.00 request limit. The default is 512 output tokens and $0.50.
The guardrail reserves the model's full documented context window plus the
output cap. The context window bounds the input of an accepted request including
system instructions, history and provider framing. The local text limit is a
separate resource limit. SOFIA does not estimate token counts from characters or
make a remote counting request. OpenAI's truncation is explicitly disabled.

At the defaults, the reservations are $0.419850 for OpenAI and $0.402560 for
Claude, even for a short prompt. Settings shows the reservation at the selected
output cap. A cap below the reservation denies dispatch. Task details distinguish
the reserved estimate, validated provider usage with calculated cost, and
uncertain usage. With missing final usage, the ledger accounts for the reserved
amount where usage is unknown and blocks the run. A reported overrun records the
reported usage and blocks the run. SOFIA's calculations use the verified rates
and can differ from the provider invoice. The request limit is per Send.
Separate, deliberate sends can accumulate charges over a session.

## Conversation, privacy and streaming

The controller captures the selected provider, limits and persona on Send.
It approves only `Action.API_REQUEST` for the exact provider/model scope in a
fresh `RunGuardrails`. One Qt worker invokes the existing adapter's
`LLMProvider.stream()`. The adapter reserves and settles the existing run budget,
uses the pinned official endpoint and closes its stream and client before the
worker finishes. There is no discovery, automatic retry, fallback or background
generation. Agent Loop demos use their separate zero-token, zero-dollar budgets.

The app composes the trusted `text_only` SOFIA Friendly system prompt on each
turn. Choose Thai (default) or English in **Settings → General**. Completed pairs
of user and assistant messages keep their roles and order. Each outgoing request
includes that history and the submitted message. A request is refused when
the chat reaches 12 completed turns or its outgoing text would exceed 64,000
UTF-8 bytes including the system prompt. The draft stays available when the
context limit is reached. Start **New Chat** to clear the conversation and draft.
Switching provider or model clears the conversation while keeping any unsent draft.

The GUI displays text as it arrives and labels partial responses after an error or
Stop. Incomplete turns stay out of subsequent requests. The on-screen response
limit is 16,384 characters. **Cancel response**, **Emergency Stop**, and closing
the window request cooperative cancellation. Visible text stops updating
immediately. Cleanup may wait for a blocked SDK operation to return. The configured
connect timeout is 5 seconds and the read/write/pool timeout is 15 seconds per
operation. They are socket timeouts. A dispatched request may still incur charges,
and the provider can continue processing after local cancellation. A new request
can start after cleanup finishes.

Submitted text goes to the selected provider and is subject to that provider's
data policies. OpenAI requests set `store=False`. SOFIA retains the conversation
locally for the current chat and exposes only status and numeric accounting in
recent task details. Credentials, prompts and replies stay out of ordinary logs.
Memory Manager remains a separate, explicit local workflow. Chat neither reads
nor writes the diary or SQLite memory. The providers receive no tools. Model
output is inert plain text and has no authority over settings or approvals.
Memory retrieval and trusted tool orchestration are separate future work
(SOFIA-025 and SOFIA-027).

Push-to-talk uses local recognition and inserts an editable draft. Review it and
click Send to transmit the edited text. **Read Aloud** is manual and available on
completed replies after enabling local TTS. For long replies the button offers the
first 1,000 characters, within the existing speech limit. Stop Speaking and
half-duplex voice protections remain available.

## Common errors

- **Credential missing or authentication failed** — check the process key and
  whether the provider account can access the model.
- **Provider extra missing** — install `.[ui,openai]` or `.[ui,anthropic]`.
- **Spending limit** — compare the selected request limit with the reservation
  shown in Settings. No request is sent when the reservation is denied.
- **Rate limit, credit limit, timeout or incomplete response** — review the error
  and accounting in Task details. SOFIA leaves retry decisions to the user.
- **Context limit** — copy any unsent draft you want to keep, then start New Chat.

## Manual Windows acceptance

1. Launch with no API keys. Confirm **Offline Mock**, send Thai and English text,
   and check the fixed replies and offline demo behavior.
2. For an explicitly authorized paid smoke test, launch with an OpenAI test
   credential and provider-side spending controls. Select the exact model,
   review the disclosure and reservation, then enable and save. Send a short
   synthetic Thai question. Verify progressive text, the model badge, and
   reported usage after completion. Send a follow-up that depends on the
   previous answer. Change the persona to English and try another turn.
3. Repeat with Anthropic. Verify the transcript clears on switch. Ask about a
   synthetic detail from the prior chat and verify it was excluded from context.
4. Start a longer reply and use **Cancel response**. Verify that no further text
   appears, the response is marked incomplete, and Task details reports known or
   uncertain usage. Repeat with Emergency Stop and then with window Close. Wait
   for cleanup and verify a fresh request can start when appropriate.
5. Dictate a synthetic sentence, verify it stays in the editor, edit it, and
   press Send. Verify only the edited text is submitted. Enable local voice
   playback, use **Read Aloud** on a complete reply, and test Stop Speaking.
6. Ask the model to open VS Code and create a file. Verify that no app opens,
   no file is written and no permission changes. Save a synthetic note
   explicitly in Memory Manager, then verify a chat cannot retrieve it and the
   Memory Manager contents stay the same after conversation.
7. Switch to Offline Mock and restart. Verify the default mode and confirm that
   the next offline submission needs neither a key nor a paid request.

The automated suite exercises the UI and both official SDKs with in-memory HTTP
transports and synthetic credentials. A live smoke test requires separate consent
and can incur charges.
