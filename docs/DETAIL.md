# Subtitle Studio

Turns pick → transcribe → translate → burn → deliver → notify into one configurable local pipeline. Every step is
executed by the bundled `engine/subtitle-run` (Python 3 + ffmpeg, no Node dependencies); the plugin only owns the
configuration, the schedule, and moving the CLI's JSON results into the settings page.

## What it does

| Step | Detail | Performed by |
| --- | --- | --- |
| Pick | Choose this round's videos from the source by size, age, already-translated state and exclude patterns | engine `plan` |
| Transcribe | Whisper (`mlx_whisper`, Metal on macOS) produces the source subtitle | engine |
| Translate | Send text to an LLM (your DeepSeek Harness models, MiniMax, Ollama, DeepSeek, or any OpenAI-compatible endpoint) using a prompt profile | engine + host relay |
| Burn | ffmpeg burns the subtitle (optionally bilingual ASS), extracts audio, trims silence | engine |
| Deliver | Name the file from `nameTemplate`, write locally or to a remote host over SSH | engine |
| Notify | QQ Bot, a command you supply, a webhook, or nothing — four channels | engine |

The plugin contributes **one settings section** (left nav "Subtitle Studio", order 50) with eight internal tabs, plus a
restricted HTTP bridge the page uses to read and write configuration, run the engine, and read logs.

## Where data lives

| Path | Contents |
| --- | --- |
| `~/.dsh/data/subtitle-studio/config.json` | the single source of truth, mode `0600` |
| `~/.dsh/data/subtitle-studio/logs/run.log` | engine run log (daily rotation, self-truncates at 5 MiB) |
| `~/.dsh/data/subtitle-studio/logs/doctor.log` | diagnostics log |
| `~/.dsh/data/subtitle-studio/logs/host.log` | bridge-level failures (write errors, launchd errors) |
| `~/.dsh/data/subtitle-studio/state/` | `run.lock`, the plugin's last-run summary, launchd record, engine config view |
| `~/.dsh/data/subtitle-studio/work/` | intermediate transcription and translation artifacts |

## The eight tabs

| Tab | What it does |
| --- | --- |
| Overview | Config state at a glance (file present, mode, parses), engine readiness, schedule state and next run, last run summary. Three actions: `Run now` (with a `dry run, no output` switch and an optional per-run cap), `Plan only` (runs `plan`, burns nothing), `Selftest` (runs `selftest`, produces a real mp4). Shows the runId while running and can cancel. |
| Engine | **One model dropdown, no plumbing.** Every model from your DeepSeek Harness configuration is grouped there alongside every model a local Ollama reports (`GET /api/tags`, refreshed when the tab opens and on demand) — the two are peers, because a model is a model. Choosing one writes `engine.kind` (`DSH` for a hosted model, `Ollama` for a local one), `engine.provider` and `engine.model` together, so there is nothing else to fill in. Picking an Ollama model reveals the only field that family needs: the Ollama address (a loopback host is probed directly; an address on another machine is used only after you save it). `Test model` sends a one-token round trip — through the host relay for DSH models, straight to `/api/generate` for Ollama ones, so probing a local model never spends hosted credits. A `Manual / other engine…` entry covers a model that is not listed yet or a different engine entirely, and reveals a free-text name box plus the **Advanced** row: engine kind, provider, base URL, keychain entry **name** (never the secret) for MiniMax / DeepSeek / OpenAI-compatible. Temperature, seed, maxTokens and batch size stay visible for every kind; the live relay status appears only when the selection is a DSH model. |
| Source and output | **Output mode** first — `Burn into video` writes the subtitles back into the picture, `Subtitle file only` leaves the picture alone and delivers a `.srt` (name taken from the template with the extension swapped). Then a directory list for multi-directory translation: each row pairs one source (kind local / SSH, path with an inline local directory browser, `include` globs, recursion) with one output (kind, path, `{stem}` name template, overwrite, SSH host/port/user/key path). Add and remove rows freely; `maxPerRun` is a shared daily cap across all rows, so earlier rows consume it first. Leave the list empty to keep the classic single-source / single-output mode, which is what the two fields below describe — they stay the defaults that new rows are pre-filled from. |
| Selection | Per-run cap, skip already translated, min/max size, max age in days, ordering, exclude globs. The plan table lists candidates, the selected ones, and why the rest were skipped. |
| Prompts | Default system / context plus a profile list (name, match globs, system, context, series); add, delete, move up and down. |
| Quality and audio | Bilingual subtitles, silence trimming, max characters per line, keep the original as SRT, **subtitle style preset** (five ready-made looks instead of hand-written ASS styles); burn switch, ffmpeg path, CRF, preset, audio policy (copy / aac / opus), font size, whisper model, language, keep intermediate audio; and a **transcription quality** card (carry the previous text as context — off by default, temperature fallback ceiling, silence threshold, confidence floor / no-speech ceiling, keep or drop end cards and repeats). Only the fields the engine actually reads with the current choices are shown: with burning off the encoder knobs disappear, the transcription engine (mlx_whisper on Apple Silicon, faster-whisper on an Intel Mac) is picked here too, bilingual burning swaps the SRT font size for the ASS one, and the notify toggles appear once a channel is picked. |
| Schedule | `launchd` (writes a LaunchAgent and fires on `HH:MM`), `dsh` (returns pasteable prompt text, does not create a task for you), `Off` (unloads the LaunchAgent); shows the plist path, load state and next run. |
| Diagnostics | Runs `doctor` and renders every check (pass / warn / fail) with its message and fix hint, failures in red; also shows engine `status` (lock, items today, log sizes). |

The footer is fixed: `Save`, `Revert`, `Reload`, plus the config path and the current issue count. A failed save flags
each field and **writes nothing**.

## Multiple source directories

`dirs` is an optional list of source→output pairs. Each entry is
`{name, source:{kind,path,ssh,include,recursive}, output:{kind,path,ssh,nameTemplate,overwrite}}`, and anything an
entry leaves out falls back to the top-level `source` / `output` defaults — so filling in only two paths per row is
enough.

- **Empty `dirs` keeps the classic single-pair mode.** Existing configurations need no migration, and the plan payload
  reports `multiDir:false` with the same `source` / `output` shape as before.
- **Selection is global.** Every directory is listed and filtered in order, candidates are merged, then re-sorted by
  `selection.order`; `selection.maxPerRun` is one shared cap, consumed by the earlier rows first. Set it to `0` for
  unlimited, or give each directory its own round if you want them to progress evenly.
- **Each pair is isolated.** Output naming uses that entry's `nameTemplate`, intermediate work directories are salted
  with the source path (so same-named files in different directories never share transcription artifacts), and the
  failure list is keyed per directory — although an entry recorded before directories existed still applies everywhere,
  so nothing silently starts re-processing.
- **A dead directory does not stop the run.** An unreachable source is skipped with a warning and reported in
  `unreachableDirs`; only when *every* directory is unreachable does the round fail with exit code `4`, matching the
  single-directory behaviour.
- Two rows writing into the same output folder is allowed but warned about: same-named files overwrite each other.

## Configuration read/write rules

- **Missing keys get defaults**: `describe` always returns defaults merged with the file content and unknown keys.
- **Unknown keys survive**: keys the plugin does not know (hand-written annotations, a future version's field) are
  written back untouched.
- **Atomic write**: temp file + `fsync` + `rename`, mode `0600`; on failure the previous file is left intact.
- **Corrupt file**: if the existing `config.json` is not valid JSON a save returns `409` and never overwrites it.
- **Reset**: writes defaults and keeps the old file as `config.json.bak`; the page reports the backup path.
- **Secrets**: `engine.apiKeyRef` accepts only a reference name — anything shaped like a real token is rejected.

## Engine contract

The plugin spawns the bundled CLI:

```bash
engine/subtitle-run doctor   --config <cfg> [--json]
engine/subtitle-run plan     --config <cfg> [--max N] [--only "<name>"] [--json]
engine/subtitle-run run      --config <cfg> [--max N] [--only] [--dry-run] [--json]
engine/subtitle-run guard    --config <cfg> [--max N] [--json]
engine/subtitle-run status   --config <cfg> [--json]
engine/subtitle-run selftest --config <cfg> [--skip-translate] [--keep] [--json]
```

Every reply is a JSON envelope: `{"ok":true,"data":{...}}` or `{"ok":false,"error":{"code","message"}}`.

| Exit code | Meaning | What the page shows |
| --- | --- | --- |
| 0 | success (including "no candidates this round") | the normal result |
| 2 | configuration error | field hints; Diagnostics names the problem |
| 3 | missing dependency (ffmpeg / whisper / filter) | red text with an `ffmpeg-full` / `mlx-whisper` hint |
| 4 | source unreachable (missing dir, SSH failure) | red text, check path or SSH |
| 5 | LLM call failed | red text, check endpoint and key |
| 6 | ffmpeg failed | red text, see run.log |
| 7 | remote write failed | red text, check SSH permissions |
| 8 | disk space low | red text |
| 9 | another instance is running (single-instance lock) | the second run is refused |

**`schedule.mode` adapter**: the engine's schema knows only `launchd` / `none` (scheduling is not its job) while the
panel offers `launchd` / `dsh` / `off`. For the latter two the plugin writes a normalized copy with `mode:"none"` to
`state/engine-config.json` and hands that to the engine, leaving your `config.json` untouched.

## Engine location and dependency self-check

- The **bundled engine** (`<package>/engine/subtitle-run`) is the default. The Engine tab shows both the bundled path
  and the effective one, tagged `Bundled` or `Custom`.
- To run your own build, set `paths.engine` to an absolute path; **Use bundled** clears it. Leaving it empty is what
  makes the package portable — another machine resolves its own bundled copy with no extra setup.
- **Checked the moment it is enabled**: two seconds after activation the plugin runs `doctor` once and caches the
  result in `~/.dsh/data/subtitle-studio/state/selfcheck.json` (6 h TTL). The Overview tab renders a Dependency check
  card with the verdict, check counts and any failing/warning rows; **Re-check** forces a fresh run.
- Covered: `ffmpeg` plus its `ass`/`subtitles`/`libx264`/`aac` filters, `mlx_whisper` (or `faster_whisper`), the CLI your
  engine kind needs, every configured source and output endpoint (SSH included), the translation relay when the kind is
  `DSH`, free disk space and state-dir permissions.

## Translation relay (engine kind `DSH`)

The bundled engine is a Python process and cannot call the host's model API directly, so with kind `DSH` translation is
relayed instead:

1. On activation the plugin writes `state/bridge.json` — `{baseUrl, tokenPath, bridgePath}` — and keeps the shared
   secret in `state/bridge-token`. Both files are `0600` and live outside `config.json`, so nothing sensitive ends up in
   a file the panel reads back or a market package ships.
2. The engine is spawned with `SUBTITLE_STUDIO_BRIDGE` pointing at that file. Each translation call re-reads it, so a
   host restart on a different port needs no re-configuration.
3. `POST /api/dsh-subtitle-studio/llm` requires the secret in `X-Subtitle-Studio-Token` (compared in constant time),
   demands `application/json`, refuses bodies over 4 MB with `413`, clamps `maxTokens` to 16 384 and `temperature` to
   0–2, and then calls `ctx.llm.stream()` with the provider and model from `engine.provider` / `engine.model`,
   returning the assembled text. A stream that never reports a finish reason, or that is cut off by the 240-second
   wall clock, is reported as a failure. Provider failures come back as `502` with the provider's message.
4. Two budgets bound the relay: at most 2 calls may be in flight (`429 busy`) and at most 120 per minute
   (`429 rate-limited`). `SUBTITLE_STUDIO_RELAY_MAX_TOKENS`, `SUBTITLE_STUDIO_RELAY_CONCURRENCY` and
   `SUBTITLE_STUDIO_SPEND_LIMIT` override them and are read per request.
5. `POST /api/dsh-subtitle-studio/model-test` is the panel's `Test model` button. It needs no token, is bounded by the
   same Host gate and the same per-minute budget, and clamps `maxTokens` to 256, so it is not a free generation
   endpoint either.

The token stops a *different user's* process; it cannot stop a process running as the same user, which is why the
route is bounded and clamped rather than trusted. The engine side is symmetric: it refuses to send subtitle text to
a relay URL that is not loopback, and it is spawned with an allow-listed environment instead of the host's full one.

Because credentials stay with the host, `DSH` needs no key reference and diagnostics do not ask for one.

## Notifications

| Channel | Required fields | Notes |
| --- | --- | --- |
| `qqbot` | `notify.qqAppId`, `notify.qqOpenId` (required), `notify.qqSecretService` (default `subtitle-studio-qqbot`) | Talks to the QQ Bot API directly: appId + clientSecret fetch an access token, which then pushes to the user behind `openid`. The clientSecret lives **only in the keychain** (or `SUBTITLE_QQ_SECRET`), never in `config.json`; `notify.qqSendPath` can still fall back to a custom script |
| `command` | `notify.command` | Any command. The text arrives on **stdin** and in `$SUBTITLE_NOTIFY_TEXT`; `{text}` / `{title}` placeholders are shell-quoted |
| `webhook` | `notify.webhookUrl` (http(s)), `notify.webhookBody` | Body defaults to `{"text": "{text}"}`; `{text}` is JSON-escaped |
| `none` | — | Silent |

Both ends validate: `qqbot` without an appId (and no fallback script), an appId without an openid, `command` without a
command, or `webhook` without an http(s) URL is rejected at the field level and never written.

`openid` belongs to the *recipient*: DM the bot once with your own QQ account, then copy it from the QQ Open Platform
event log (the notification card has a helper that extracts it from a pasted message JSON). "Send test message" in the
card verifies the whole path; a missing secret shows up as a `doctor` error instead of a silent skip. A failed notification never changes the pipeline result (the engine logs and moves on).

## Light and dark

No hardcoded colours: all 13 come from host tokens (`--dsw-alias-*`), and `design-platform.css` defines each token
twice — light on `body`, dark on `body[data-ds-dark-theme]` — so the panel follows the theme automatically. The panel
also injects a scoped sheet: keyboard focus uses a primary-colour ring (no more `outline: none`), native controls
declare `color-scheme: light dark`, and selections use the hover token.

## Scheduling

- **launchd**: writes `~/Library/LaunchAgents/<label>.plist` whose `ProgramArguments` are the engine CLI
  `guard --config <cfg>`, builds `StartCalendarInterval` from `schedule.time` (`HH:MM`), then `launchctl bootout` +
  `bootstrap gui/<uid>`. The plist is linted with `plutil -lint` first; a failed bootstrap restores the previous plist
  and reports the error. Re-saving the same schedule is idempotent (`unchanged`); changing the label unloads the old
  agent.
- **dsh**: returns pasteable prompt text only. DSH schedules are prompt deliveries that spend model tokens on every
  firing, so whether to create one stays your call. One prompt is emitted per trigger point
  (`schedule.time`, the primary one, plus `schedule.guardTimes`, up to 11 extras). In launchd mode the same set
  is written into the plist as extra `StartCalendarInterval` entries, so a missed night gets caught up; every prompt runs the same
  idempotent `guard`, so extra points can only retry a missed day — they never double-process one.
- **Off**: unloads the agent and removes the plist.

## Security model

- Every bridge route sits behind a **Host fence**, modelled on the harness's own local API bridge: `Host` must be a
  loopback name or the exact authority the plugin published when it loaded, `sec-fetch-site: cross-site` is rejected,
  an `Origin` that is present must match that authority, and an opaque `Origin: null` is rejected — all with `403`.
  This is what blocks DNS rebinding: a page served from `http://evil.example:8123` that later resolves to
  `127.0.0.1` carries its own host and origin, and can no longer reach `save` (which feeds `notify.command`) or
  `schedule`. Without the Host check, matching `Origin` against `Host` alone would have been satisfied.
- No route runs a shell. Path arguments are used for directory enumeration and as engine arguments only.
- Directory enumeration is read-only, realpath-normalized, capped at 500 entries with an 8-second timeout, and refuses
  `/System`, `/etc`, `/private/var/db`, `~/.ssh`, `~/Library/Keychains` and similar system or credential directories.
- Logs are read from an allow-list: `*.log` under `logs/` whose realpath stays inside that directory; any other name is
  a hard `404`.
- Bodies must be JSON: a request with a foreign `Content-Type` is refused (`415`), and one over the cap gets a clean
  `413` instead of a reset socket.
- `state/` is created `0700`; `bridge-token` and `bridge.json` are `0600`. Neither value is ever returned by
  `describe`, written to `config.json`, or printed to a log.

## Log viewing

The `logs` route reads `~/.dsh/data/subtitle-studio/logs/<name>.log`, 200 lines by default and at most 2000. Before a
file exists it falls back to the output the plugin captured in memory for the current or last job — only for `run` and
`selftest`; any other name is a `404`.

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| `ffmpeg.filters` fails in diagnostics | Homebrew's plain `ffmpeg` ships without libass: install `ffmpeg-full`, or point `burn.ffmpeg` at a binary with the `ass`/`subtitles` filters |
| `mlx_whisper` warning | `pip3 install --user mlx-whisper`; a CPU whisper build is fine to ignore it |
| Ollama unreachable | start `ollama serve`, or point `engine.ollamaHost` at the real address |
| Run seems to do nothing | check the lock in Diagnostics; exit code 9 means the previous round is still running |
| Save returns 409 | the existing `config.json` is not valid JSON — fix it by hand or press Reset |
| launchd never fires | `launchctl print gui/$(id -u)/<label>`; the plist holds the package's absolute engine path, so re-save the schedule after moving the package |

## Terminology glossary

`prompts.profiles[].series` names a JSON glossary under
`~/.dsh/data/subtitle-studio/series/<name>.json` (override with
`SUBTITLE_STUDIO_SERIES_DIR`). The engine reads it at translate time, renders it into the
prompt as a proper-noun table plus an `ASR mis-hearing → correct` fix list, and appends the
`postFix` replacements to the translated text.

The package ships no vocabulary of any specific work — only the loader
(`engine/vendor/series_context.py`) and a schema example
(`engine/vendor/series.example.json`). If the named file is absent or unreadable the engine
prints one log line and translates without a glossary; a run is never failed by vocabulary.

## Output mode, timeline alignment and transcription quality

**Output mode** (`output.mode`) is the one switch that decides what a round produces.

- `burn` — the translated subtitles are burned into the picture and the finished video is delivered (what the plugin
  has always done).
- `srt` — the picture is left untouched and only the subtitle file is delivered. The name comes from the same
  `{stem}` template as the video, with the extension swapped, so `ep01_zh.mp4` becomes `ep01_zh.srt`.
- **Older configurations keep working.** A configuration written before this switch only has `burn.enabled`; the panel
  derives the mode from it, and saving writes both keys in sync. The engine falls back the same way, so a config file
  edited by hand still behaves.
- The "already translated, skip it" check uses the very same name the mode produces, so switching to `srt` stops the
  pipeline from reprocessing files it thinks are done.
- **Bilingual follows the mode.** With bilingual subtitles on, the delivered `.srt` in `srt` mode is the bilingual one
  (translation above, original below) rather than the translation-only file — the file you get is the file you asked for.

**Timeline alignment.** When a source file's audio starts a couple of seconds after the picture (a common trait of
broadcast rips and remuxes), `ffmpeg` drops that gap while extracting the audio — the subtitles then come out early by
exactly the gap. The engine measures it instead of guessing: it probes the audio and video stream start times and the
extracted audio's duration, and works out how much silence the container implied but the extractor threw away. That
silence is prepended to the extracted audio, so the transcript lines up with the picture. Differences below 0.2 s are
left alone (they are inaudible for subtitles), and a negative measurement is only reported, never "corrected".

**Transcription quality** (the anti-hallucination card) covers what Whisper tends to do to long recordings:

- *End cards and thank-you lines* — "ご視聴ありがとうございました", "字幕制作…", "thanks for watching" and similar
  are dropped. You can keep them with a switch.
- *Repeated lines* — a phrase repeated three or more times in one block is collapsed back to one; a legitimate repeated
  line (a chorus, a chant) is left alone unless it is an exact copy of the previous block, in which case the timings are
  merged.
- *Low confidence* — blocks whose average log-probability falls below the floor, or whose no-speech probability rises
  above the ceiling, are discarded. Segments that do not report confidence are never dropped by this rule.
- *Carry the previous text as context* — off by default, because carrying context is what makes Whisper continue
  a sentence it never heard. Turning it on is a per-round choice.
- *Temperature fallback* — with the fallback ceiling at `0` the transcription is a single, most-likely pass. Raise it and
  low-confidence stretches are re-decoded with a rising temperature ladder.

**Subtitle style presets** replace hand-written ASS styles with five ready-made looks: white with a black outline
(the default, and byte-for-byte the same style line the plugin used to hard-code), bold yellow, white at the top for
documentaries, white on a black box, and a cinema look. A preset drives both paths: the ASS file used for bilingual
subtitles and the `force_style` string used when burning plain subtitles.

## Known limits

- With engine kind `DSH` the provider and model come from your own DSH configuration; a model that has not been
  published to `ctx.llm` yet can still be typed in through `Custom model name…`, and the plugin never refuses a name
  outside that list — the model namespace belongs to the provider, not to us.
- The plugin does not install or enable itself: use `plugin_manager install_bundle`.
- The directory browser covers local paths; SSH paths are typed by hand.
- `maxPerRun` is one shared cap across every configured directory, consumed in list order. Directories at the end of a
  long list can starve if the earlier ones always have candidates.
- `--only` / the `only` field matches on filename, so a substring that appears in same-named files across directories
  forces all of them.
- Scheduling writes a `launchd` LaunchAgent, so automatic runs are macOS-only; the `dsh` mode hands you prompt text to
  paste rather than creating a task behind your back.
