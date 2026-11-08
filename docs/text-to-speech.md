# Local text-to-speech

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[ui,tts]"
.\.venv\Scripts\python.exe -m sofia.ui
```

Open Settings, check **Enable local voice playback**, select language and voice,
then Save. Each visible Sofia response has **Read Aloud**. **Test selected voice**
previews a synthetic greeting using unsaved controls. **Stop Speaking** cancels
current and pending speech; **Clear pending speech** leaves current audio alone.
Nothing speaks at startup. There is no auto-speak. Preferences are session-only.

## Backend and voices

The `windows-local` backend uses installed Windows WinRT voices through the
MIT-licensed PyWinRT 3.2.1 projections. The `tts` extra installs SpeechSynthesis,
Storage.Streams, Foundation and Foundation.Collections only on Windows. This
version provides Python 3.11, 3.13 and 3.14 Windows wheels; local native validation
used Python 3.14.3. Other platforms and missing extras report unavailable without
breaking the GUI. The ordinary CLI stays headless and imports no audio libraries.

QtTextToSpeech 6.10.3 and its SAPI/WinRT plugins were investigated against installed
signatures and source. Installed voices produced audible Thai/English, but
engine-managed completion was intermittent during repeated GUI playback. The
implemented backend therefore uses Windows synthesis directly and a SOFIA-owned
QAudioSink. There is one implemented local backend; the typed interface allows
additional trusted adapters without coupling UI/queue behavior to PCM.

Settings lists only discovered Thai (`th-TH`) and English (`en-*`) voices. IDs
are hashes of actual engine voice IDs, never executable paths. Both voice ID and
exact language are checked again by the synthesis worker. Missing voices and
languages fail without fallback or translation. Names and gender labels come
from Windows metadata. A reported female voice is preferred when selecting a
language; when none is reported, available alternatives and the limitation are
shown. Metadata does not prove naturalness or pronunciation quality.

For missing voices, explicitly install a Windows language's text-to-speech
feature in **Settings → Time & language → Language & region → language options**,
then restart SOFIA. Windows versions differ; consult Microsoft's
[supported voices and installation guidance](https://support.microsoft.com/en-us/accessibility/windows/narrator/appendix-a-supported-languages-and-voices).
Narrator or SAPI availability alone does not establish WinRT availability. SOFIA
does not install voices or modify Windows permissions. OS voices remain subject
to Microsoft's applicable terms; none are bundled or redistributed.

Language selection is explicit. Thai-only text requires a Thai voice, and
English-only text requires an English voice. Mixed text such as
“สวัสดีค่ะ Welcome back to Project Sofia.” is rejected with a clear message.
No automatic language switching, translation or Thai word segmentation is
claimed. The existing bilingual Mock reply cannot be read as one utterance;
use the separate language-specific Settings tests.

Speaking speed is **0.75x–1.25x**, shown as −25% through +25% relative to normal.
The adapter maps this to Windows' actual speaking-rate multiplier, rather than
assuming Qt's numeric scale means the same thing. Speech volume is 0–100% on
SOFIA's QAudioSink; Windows master volume is untouched. Pitch/style/emotion
controls are not offered, and non-null pitch settings are rejected. Already
queued requests retain the settings selected when requested. Disabling playback
cancels all speech. No settings, messages or audio are written to disk.

## Interface, queue and state

`sofia.tts.models` defines immutable Voice, Capabilities, TTSConfig and private-repr
TTSRequest values. `TTSEngine` exposes capabilities and asynchronous start,
stop and close. Metadata describes installed languages/voices, rate/pitch ranges,
PCM versus engine-managed playback, incremental support, interruption and
networking. Only a trusted application factory chooses engines; providers and
tools cannot register engines, supply libraries, models, commands or services.

An engine must validate before dispatch. An exception from start means no work
remains active. After dispatch, fixed events arrive on the owner thread. Terminal
events acknowledge quiescence, after which close is safe and nonblocking.
Future neural or engine-managed adapters can own their own workers/playback,
provided they honor these limits and cancellation semantics.

PlaybackQueue is FIFO with **one active**, **three pending**, **1,000 Unicode
code points per utterance** and **3,000 aggregate active/pending code points**.
Each request has an opaque ID. Current/pending duplicate IDs are rejected. New
requests append without interrupting prior speech. The trusted controller also
supports stop-current and replace-pending; replacement leaves current audio
alone. Cancelled, replaced or failed items are never replayed. Failed items
release their slot and advance the queue.

Lifecycle: idle → queued → preparing → synthesizing → playing → completed,
with stopping → cancelled, unavailable and sanitized failures. Synthesizing is
reported during Windows synthesis; Playing begins only when QAudioSink reports
Active. Completed requires a drained sink with the source buffer at its end.
Rejected requests appear beside the active status without replacing it. Both
request ID and engine identity reject stale controller callbacks; the synthesis
result also carries its session ID.

Each dispatched lifecycle has a **120-second** deadline. Timeout clears pending
speech and requests Stop. If acknowledgement takes more than **five seconds**,
`stop_timeout` remains visible and new speech/microphone capture stay blocked.
Ownership is retained until native work finishes. A defective native call can
delay shutdown; there is no unsafe thread termination or false stop claim.

## Audio, threads and cancellation

One owned QThread performs local WinRT synthesis and reads the returned WAV into
bounded memory. COM initialization/uninitialization occurs on that same worker
thread. Windows async operation status is polled there with short sleeps and
cancellation checkpoints; no asyncio socket pair, subprocess or networking is
needed. Stop requests cancellation of an outstanding Windows operation and
invalidates all results. A late result cannot reach the speaker.

The GUI receives immutable PCM after both result delivery and native thread exit.
It checks a zero-time QThread join before deletion, retrying through Qt's event
loop. All QAudioSink/QBuffer ownership and widget updates stay on the GUI thread.
No blocking GUI joins, unmanaged/daemon threads or QThread.terminate() are used.

The PCM boundary accepts only finite-by-construction signed 16-bit integer audio,
8–48 kHz, one/two channels, nonempty frame-aligned bytes, at most **60 seconds**
and **12 MB**. WAV compression/other sample widths, truncation, malformed headers,
oversized output and unsupported device formats fail. Windows' stream size is
checked before allocating/reading. There is no sample-rate relabeling; unsupported
formats are rejected instead of silently resampled. No float NaN/Inf can enter
this integer-only interface. No arbitrary file/audio URL is accepted.

SOFIA requires an available default output endpoint supporting the actual PCM
format. Endpoint loss cancels output. Device errors are sanitized. Zero-argument
`@Slot()` handlers query sink state and validate sender identity, avoiding the
PySide QAudio::State conversion defect seen in Issue #13. The API is compatible
with PySide6 6.10.x; it uses no Qt 6.11 callback APIs.

Stop resets QAudioSink to discard buffered audio immediately, closes its buffer,
clears pending speech and waits for any synthesis worker before acknowledging
termination. Emergency Stop preserves existing agent, approval and STT behavior
and also cancels TTS. Starting a task, task cancellation, New chat and application
close stop speech. Close defers while any owned native work remains active.
OS/device buffers can add acoustic latency after an application-level reset;
reported Stop latency measures acknowledgement, not sound at the ear.

Voice interaction is half-duplex. Hold Mic first cancels TTS. If acknowledgement
is immediate, capture starts; otherwise the user must hold Mic again after Stop
finishes. No microphone starts later after button release. Playback is blocked
during capture/transcription, tasks, approval dialogs and shutdown. STT drafts
still require manual Send; only manual Approve/Deny decides protected actions.
There is no always-listening mode or acoustic echo cancellation. Speakers,
room echo, OS loopback and other apps can still enter a microphone; headphones
reduce that risk.

## Security and privacy

TTS uses no cloud service, model download, paid API, browser, shell or tool. It
opens no microphone and saves no WAV/MP3/temp recordings or transcripts. Only
visible Sofia responses have Read Aloud; user drafts, approval details, hidden
prompts, raw traces and tool observations are not connected to playback.

Validation rejects unsupported types, blank/oversized text, invalid Unicode,
dangerous controls, unsupported scripts/locales, markup/SSML and leading
path/URL requests. Other Markdown/URL mentions are plain speech text. The Windows
API receives plain text through SynthesizeTextToStreamAsync, never SSML or file
instructions. Speech cannot grant permissions, approve an action or execute tools.

Logs contain fixed tts_requested, tts_queued, tts_started, tts_completed,
tts_cancelled, tts_unavailable and tts_failed events only. They contain no speech
text, audio, raw exceptions, native device IDs or personal paths. Session text
and audio are released for allocator reuse; this is not cryptographic erasure.
OS components and other same-user processes remain outside this boundary.

## Licensing and neural investigation

[PyWinRT SpeechSynthesis 3.2.1](https://pypi.org/project/winrt-Windows.Media.SpeechSynthesis/3.2.1/)
is MIT-licensed and supplies Windows wheels for the supported Python versions.
The three companion namespace packages use the same version. Qt/PySide remains
subject to [Qt for Python licensing](https://doc.qt.io/qtforpython-6/licenses.html).
Neither license grants rights to redistribute Windows voices.

[OHF Piper](https://github.com/OHF-Voice/piper1-gpl) was investigated, not
installed/integrated. Its [1.8.0 package](https://pypi.org/project/piper-tts/) uses
GPL-3.0-or-later and offers a Windows x64 cp39-abi3 wheel. That suggests Python
3.11/3.13/3.14 ABI compatibility, but dependencies, CPU/memory performance and
cancellation were not tested here. Its chunked PCM contract could fit a future
adapter with independent cancellable playback and bounded synthesis.

The [Thai tsync2 model card](https://huggingface.co/rhasspy/piper-voices/blob/main/th/th_TH/tsync2/medium/MODEL_CARD)
requires Piper >=1.8 and specifies 22,050 Hz and a **CC BY-NC-SA 3.0
(non-commercial)** dataset. Its model is about 63 MB. The repository-wide MIT
label does not remove dataset restrictions; exact model/derivative and individual
English voice licenses need review before redistribution. No English neural voice
is advertised as verified female. Installed Windows voices cover real Thai and
English here, so no neural runtime/model is necessary. There are no SOFIA neural
installation commands or automatic model downloads.

API review included [Qt 6.10 QTextToSpeech](https://doc.qt.io/qt-6.10/qtexttospeech.html),
[PySide6 signatures](https://doc.qt.io/qtforpython-6.10/PySide6/QtTextToSpeech/QTextToSpeech.html),
[QAudioSink](https://doc.qt.io/qt-6.10/qaudiosink.html), Qt SAPI/WinRT source,
and installed PyWinRT stubs. Windows native voice rights are separate from these
binding and engine licenses.

## Verification

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[dev,ui,stt,tts,browser,openai,anthropic]"
.\.venv\Scripts\python.exe -m pytest tests/tts tests/ui/test_tts.py tests/ui/test_tts_native.py -v
.\.venv\Scripts\python.exe -m pytest -v
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m ruff format --check .
.\.venv\Scripts\python.exe -m mypy src/sofia
.\.venv\Scripts\python.exe -m pip check
```

Ordinary tests use fake synthesis, speakers and devices and retain the global
socket ban. They cover PCM/WAV bounds, queue/configuration, lifecycle/stale events,
errors, worker cancellation/cleanup, UI responsiveness, privacy, approval isolation
and half-duplex STT. No ordinary test opens speakers or downloads voices/models.

```powershell
$env:SOFIA_TTS_NATIVE_TEST = '1'       # installed voice discovery, no speech
.\.venv\Scripts\python.exe -m pytest tests/ui/test_tts_windows.py -m tts_native -v
$env:SOFIA_TTS_PLAYBACK_TEST = '1'     # explicitly permits synthetic speaker output
.\.venv\Scripts\python.exe -m pytest tests/ui/test_tts_windows.py -v -s
Remove-Item Env:SOFIA_TTS_PLAYBACK_TEST
Remove-Item Env:SOFIA_TTS_NATIVE_TEST
```

Native tests separately check actual English/Thai playback state, interruption
during synthesis/playback, FIFO, queue limits, Emergency Stop and cleanup. Missing
voices are explicit skips and do not count as audible acceptance. No test can
establish that a human heard sound.

For the manual Windows demo, launch the GUI; confirm silent startup; discover
voices; test English “Hello! I'm Sofia. How can I help you today?”; change speed
and volume; stop mid-utterance; queue several previews and clear pending; test
Emergency Stop; select Thai and test “สวัสดีค่ะ ฉันโซเฟีย มีอะไรให้ช่วยไหมคะ”.
Check mixed-language rejection, Read Aloud on a single-language response,
push-to-talk handoff, approval blocking, output disconnection and close during
speech. Use synthetic phrases only, no paid calls or public network in the demo.
Record audible quality separately from backend state and application stop timing.

### Local voice discovery

Windows/Python 3.14.3, PySide6 6.10.3, PyWinRT 3.2.1: Microsoft David (en-US,
Male), Microsoft Zira (en-US, Female), Microsoft Mark (en-US, Male), Microsoft
Pattara (th-TH, Male). Three output endpoints were discovered. No Thai female
voice was found. Native Thai male speech is available without another model.
The user confirmed hearing both initial greetings clearly. This establishes
audibility/intelligibility of those samples, not formal pronunciation or
naturalness scoring. Final automated/native results and CI are recorded in the PR.
