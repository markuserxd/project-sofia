# Offline push-to-talk speech input

Issue #13 adds microphone input to the existing PySide6 composer. Choose Thai
(`th`) or English (`en`), hold **Hold Mic**, speak, then release. Keyboard users
can focus the button and hold Space. There are no global keyboard hooks. A
visible recording label and elapsed time appear only after Qt reports active
capture. Release outside the button also stops capture. Losing window activation
or microphone-button focus while capturing cancels and discards that recording.

Transcription is **draft text only**. It appends to the current composer with a
space when needed, preserving edits made during inference. Review/edit the text
and press Send yourself. Combined drafts exceeding 4,096 Unicode characters are
rejected without insertion or truncation. Enter/Shift+Enter, settings, themes,
task history, manual approvals and the original CLI retain their existing behavior.

## Installation and deliberate model setup

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[dev,ui,stt,browser,openai,anthropic]"
```

`stt` installs faster-whisper 1.2.1 or compatible 1.x, NumPy 2.x and SciPy 1.x.
Qt Multimedia is already included in the existing PySide6 6.10.x extra. No CUDA,
GPU, paid API or separate system FFmpeg installation is required. The CLI and
imports of `sofia`, `sofia.ui` and `sofia.speech` do not load models or microphones.
Dependencies are optional; missing STT packages produce a fixed installation hint.

On Ubuntu, install `libegl1 libopengl0 libpulse0` through the system package
manager before launching Qt or running offscreen tests. Qt Multimedia links
`libpulse.so.0` even when synthetic test devices are used; no running audio server
or physical microphone is needed for the mock tests. CI installs these libraries.

Start with [Systran/faster-whisper-base](https://huggingface.co/Systran/faster-whisper-base).
This is the multilingual base Whisper model converted to CTranslate2, licensed
MIT. Its model repository is approximately 148 MB (weights about 145 MB).
[Original Whisper](https://github.com/openai/whisper#available-models-and-languages)
lists base as 74 million parameters, multilingual, with approximately 1 GB of
GPU memory for its implementation. That is **not a CPU memory measurement for
SOFIA**: CPU int8 runtime memory includes libraries, model and feature/VAD arrays
and must be measured on the target machine. Leave at least a few GB free for a
practical initial setup. Thai quality varies; base does not guarantee accurate
Thai. A complete multilingual `small` model is an optional accuracy upgrade with
greater disk/memory use and latency. English-only `.en` models are rejected.

Download deliberately on a connected machine, separately from SOFIA. This command
uses networking only because you explicitly invoke the Hugging Face installer:

```powershell
# Choose an empty local directory outside the Git checkout.
.\.venv\Scripts\hf.exe download Systran/faster-whisper-base --local-dir C:\SOFIA-models\whisper-base
$env:SOFIA_STT_MODEL_DIR = 'C:\SOFIA-models\whisper-base'
.\.venv\Scripts\python.exe -m sofia.ui
```

For reproducible setup, inspect the model publisher's revision/history and add
`--revision <reviewed-commit-sha>` to the download command. Verify the downloaded
files against that publisher revision, retain the license/provenance, and transfer
the complete directory to the offline machine. SOFIA requires nonempty local
`model.bin`, `config.json`, `tokenizer.json` and `vocabulary.txt`; files may not
resolve outside that directory. A supplied `preprocessor_config.json` should come
from the same trusted conversion. No scripts from model packages are executed.
This structural validation does not authenticate weights or defend against a
malicious native model. Use trusted publisher files on a local drive, with normal
user permissions; never select models from untrusted prompts or network shares.

`SOFIA_STT_MODEL_DIR` must be an absolute, trusted local path. It is read only at
GUI construction and never accepted from audio, transcripts or a model response.
Restart the GUI after changing it. No model directory is bundled in Git or CI.
No model installation command runs at startup or on press/release. Missing,
incomplete, corrupt, English-only or incompatible models fail locally with a safe
message; there is no cloud fallback.

The adapter passes a validated local directory and `local_files_only=True` to
[faster-whisper](https://github.com/SYSTRAN/faster-whisper/tree/v1.2.1).
Its source can fetch a tokenizer when `tokenizer.json` is absent, so SOFIA
requires that file **before constructing the model**. VAD uses the package's
bundled Silero ONNX model. ONNX Runtime telemetry is disabled before VAD use.
Verify the prepared environment with networking disabled and the real-model
tests below before relying on it offline.

## Capture and audio processing

`speech/capture.py` owns one `QAudioSource` and its returned pull-mode QIODevice
on the GUI thread. It enumerates inputs at startup without opening the microphone.
On explicit press it validates the current system default microphone, prefers
mono signed 16-bit PCM at 16 kHz and checks `isFormatSupported`. Otherwise it uses
the device's preferred format. USB, integrated and headset inputs are supported
when Qt exposes a mono/stereo format: UInt8, Int16, Int32 or Float at 8–96 kHz.
Unsupported formats/layouts fail safely; there is no device-selection UI. Change
the default microphone through Windows settings before a new session.

The adapter uses the documented
[Qt 6.10 QAudioSource QIODevice API](https://doc.qt.io/qtforpython-6.10/PySide6/QtMultimedia/QAudioSource.html),
not the newer callback API. GUI-thread readyRead handlers copy bounded chunks;
they do not infer, resample, access disk or touch a native real-time callback.
The Qt buffer is requested at 250 ms (the backend may choose its actual size).
The in-memory PCM buffer is capped by sample count at 15 seconds: at most
11,520,000 bytes for 96 kHz stereo 32-bit input. Monotonic timers separately
enforce the 15-second capture limit; initialization without active capture fails
after 3 seconds. Full buffers stop capture; excess data is rejected/discarded.
Malformed final frame boundaries and reported device/overflow failures are errors,
never silently transcribed. Qt cannot identify every hardware-level dropped frame.

The inference worker validates 0.3–15 seconds of complete frames, decodes PCM,
checks finite amplitudes within [-1, 1], averages stereo, and performs actual
polyphase antialiased resampling with `scipy.signal.resample_poly` to 16 kHz.
It never relabels native samples. The model receives a bounded contiguous mono
float32 NumPy array (maximum 240,000 samples). Only near-digital silence is
rejected by amplitude; local VAD handles ordinary quiet speech. Noise can still
hallucinate text. Unicode is validated as strict UTF-8 with conservative trimming;
empty text, invalid types/control characters and oversized output are rejected.
Language is explicit; transcription uses `task="transcribe"`, never translation.
Mixed Thai/English recognition is best effort, especially for short clips.

## States, cancellation and ownership

The controller uses ready/unavailable → initializing → recording → transcribing
→ completed, or cancelled/fixed error codes. Each session has a fresh UUID.
Transcribing includes visible first-use model loading. A single recognizer is
reused serially, retaining one CPU int8 model; loading is never done on the GUI
thread. Inference uses four CPU threads, one worker, deterministic decoding,
bounded token/segment/text output and no previous-text prompt conditioning.

The QObject worker lives in an owned QThread. Results are validated before queued
delivery to GUI-thread slots. Session identities and cancellation gates reject
late results. A cancelled worker occupies the one inference slot until it exits;
repeated presses cannot create unlimited workers. Thread deletion waits through
nonblocking `wait(0)` checks for native thread cleanup, matching the existing UI
worker lifecycle. There is no QThread.terminate or blocking GUI-thread join.

**Cancel voice**, Escape on the Mic control, Emergency Stop, starting an existing
task, and application close discard capture and invalidate pending text. Sensitive
approval/task states block new capture. Cancelling inference or exceeding its
120-second deadline prevents insertion immediately, but cannot interrupt an
already-running native model load/inference call. Cancellation is checked around
loading and each lazy segment. The window stays responsive and defers close until
the owned worker returns; a blocked native call can delay shutdown. The UI does
not claim native inference was forcibly stopped. Process termination cannot
guarantee cleanup. Python/native allocator memory is released for reuse, not
cryptographically erased; the OS may page memory.

## Windows permissions and errors

In Windows 11, open **Settings → Privacy & security → Microphone**; enable
microphone access and **Let desktop apps access your microphone**. Windows 10
uses **Settings → Privacy → Microphone**. See
[Microsoft's permission instructions](https://support.microsoft.com/en-us/windows/privacy/turn-on-app-permissions-for-your-microphone-in-windows).
SOFIA does not change privacy settings or request elevation. Qt permission denial
is shown explicitly when reported. On Windows some backends report denied access
only as OpenError, indistinguishable from hardware/open failures; the safe message
asks the user to check both device and privacy settings. No automatic retry starts
recording after a delayed permission change. Press again deliberately.

No microphone, disconnect, format failure, overflow, too-short recording, silence,
model/backend errors, invalid output, timeout and cancellation all have fixed
friendly messages. Raw exceptions, model paths and device IDs are not displayed.

## Security and privacy

Audio and recognized text are untrusted input. Push-to-talk reduces accidental
input but does not authenticate a speaker: a nearby person or recording can
provide words. Speech is never an authorization channel. Even “Approve everything”
or “Delete all files” only becomes editable draft text. Speech code has no agent,
tool-registry or PermissionPolicy interface. It cannot send chat, approve a dialog,
launch a process/browser, write a file, change settings or activate paid APIs.
Existing manual Approve/Deny controls remain authoritative.

Microphone capture is closed at startup and between sessions. There is no
continuous/background listening, wake word, upload, cloud STT, recording archive,
audio playback, telemetry integration or automatic model download. Audio stays in
memory; no WAV/temp file is created. Draft text creates no history until Send.
SOFIA speech code logs neither audio nor transcripts (it currently emits no speech
log records); faster-whisper diagnostics are disabled to prevent its DEBUG text
from leaking recognized speech. Do not attach personal recordings or full
transcripts to bug reports. OS audio components and other same-user processes are
outside this application's control. Future text-to-speech would be a separate,
explicit playback feature; this input interface grants it no authority.

## Test levels and actual validation

Level 1 is synthetic/offline: bounded PCM, numeric conversion, fake microphone,
mock model factory and recognizer, Qt offscreen input, cancellation races,
responsive GUI timers, worker cleanup, approval isolation and privacy. The global
test socket ban remains active. CI installs STT libraries but downloads no models.

```powershell
.\.venv\Scripts\python.exe -m pytest tests/speech tests/ui/test_speech.py tests/ui/test_speech_capture.py -v
.\.venv\Scripts\python.exe -m pytest -v
.\.venv\Scripts\python.exe -m ruff check .
.\.venv\Scripts\python.exe -m ruff format --check .
.\.venv\Scripts\python.exe -m mypy src/sofia
.\.venv\Scripts\python.exe -m pip check
```

Level 2 requires a deliberately installed model and **consented local** mono or
stereo 16-bit PCM WAV samples, each 0.3–15 seconds. Keep voice files outside Git.
Disable networking at the OS level too; the pytest socket ban still applies.

```powershell
$env:SOFIA_SPEECH_MODEL_TEST = '1'
$env:SOFIA_STT_MODEL_DIR = 'C:\SOFIA-models\whisper-base'
$env:SOFIA_STT_THAI_WAV = 'C:\consented-samples\thai.wav'
$env:SOFIA_STT_ENGLISH_WAV = 'C:\consented-samples\english.wav'
.\.venv\Scripts\python.exe -m pytest tests/speech/test_real_model.py -v
```

These sanity checks require nonempty language-appropriate text; they do not
measure word error rate. Inspect the results privately during the manual GUI test.
The test deliberately does not print personal text or create audio artifacts.

Level 3 is real Windows hardware. The optional command below explicitly consents
to a half-second in-memory capture, exercises actual QAudioSource and discards it;
it performs no inference. It is skipped during ordinary CI.

```powershell
$env:SOFIA_MICROPHONE_TEST = '1'
.\.venv\Scripts\python.exe -m pytest tests/ui/test_speech_microphone.py -v
# Then perform the interactive bilingual test:
.\.venv\Scripts\python.exe -m sofia.ui
```

1. Select Thai and hold Mic: “สวัสดีโซเฟีย วันนี้อากาศเป็นอย่างไร”. Release and
   check the visible recording/transcribing states and recognizable Thai draft.
2. Edit it; confirm no task/message starts until you manually press Send.
3. Select English: “Hello Sofia, can you help me today?”. Repeat release/review.
4. Try “โซเฟีย ช่วยเปิด Project Sofia หน่อย”. Assess mixed-language differences;
   confirm no desktop action happens. Punctuation/capitalization need not match.
5. Preserve existing draft text and edits made while transcribing. Try silence,
   short taps, the 15-second limit, Space activation and release outside the button.
6. Test focus loss, Cancel voice, Emergency Stop during capture/inference, closing
   during inference, rapid presses and a fresh session after cancellation/error.
7. Test unplugging the active microphone and Windows permission denial. Restore
   permissions/device deliberately; confirm safe recovery and no hidden restart.
8. Confirm microphone is disabled during protected approval; only manual buttons
   can decide. Check Light/Dark themes and responsive Settings while transcribing.

Record host/Python/Qt/model revision, the exact commands, actual recognized text
and differences privately, latency/memory if measured, and any freezes or QThread
warnings. Mock Thai/English fixtures prove integration, **not model accuracy**.

### Implementation validation record

Local host: Windows, Python 3.14.3, PySide6 6.10.3. Optional dependencies installed
successfully: faster-whisper 1.2.1, CTranslate2 4.8.5, ONNX Runtime 1.31.1,
NumPy 2.5.4 and SciPy 1.18.1. Their native imports passed. This validates package
installation/import compatibility, not inference accuracy or throughput.

The focused commands (including `tests/test_speech_startup.py`) passed **83 tests,
3 skipped**. The full `python -m pytest -v` suite passed **1,237 tests, 7 skipped**.
Ruff lint/format, mypy (54 modules), pip check, diff whitespace checks, and all
original CLI commands passed. Tests used a repository-local `--basetemp=.tmp/...`;
final suites ran outside Windows sandbox filesystem virtualization after a
sandboxed run failed to create pytest temporary directories. No existing checks
were weakened or skipped to obtain these results.

A native Windows GUI smoke check opened the real window, verified the layout,
accessible Mic/language/status controls and selector choices, and closed it
without QThread warnings or stderr diagnostics. Capture was not activated.
Offscreen tests exercised recording, errors, bilingual draft insertion, editing,
release outside, Space, cancellation, Stop, approval isolation and worker cleanup
using synthetic audio and recognizers only.

The two real-model checks and one real-microphone check were explicitly skipped:
there is no installed model/consented Thai and English sample set for this run,
and no user-operated microphone capture was performed. No real Thai/English
recognition accuracy, real microphone success, offline model load under disabled
networking or CPU inference performance is claimed. Complete Levels 2 and 3
above before treating those hardware/accuracy acceptance items as validated.
GitHub Actions validation is reported on the implementation PR separately.
