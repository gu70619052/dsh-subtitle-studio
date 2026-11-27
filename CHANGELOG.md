# Changelog

All dates are the day the release was tagged. Config keys added by a release keep
working on older files: everything is deep-merged over defaults, so an upgrade
never needs a migration step.

## v1.6.0

- **New: the plugin remembers what it has already translated.** Until now
  "already translated" meant "the expected filename is sitting in the *current*
  output folder" — so pointing the output at a new folder, or switching between
  burning and subtitle-only, quietly re-translated the whole library. Every
  finished file is now recorded in `state/translated.json` (keyed by source name,
  with the directory tag for multi-folder setups, holding the produced path, the
  output mode and the time), and the record is consulted before the folder check.
  A hit is not taken on faith: the folder is listed once more, and a record whose
  product has been deleted counts as untranslated — re-translating costs the
  user quota, believing a stale record costs them a whole film.
- **New: the plugin does not translate its own output.** With source and output
  pointed at the same folder, `X_zh.mp4` used to be picked up again and written
  as `X_zh_zh.mp4`, which the overwrite policy cannot catch (the name really is
  new). Files that match the name template and already sit in the output folder
  are now skipped as `own-output`, with the reason shown in the plan;
  `selection.skipOwnOutputs` turns it off for anyone who genuinely names sources
  that way.
- **New: a missing dependency can be installed from the panel.** Each `doctor`
  row that has a known fix gets a button that runs exactly one command from a
  fixed table (`brew install ffmpeg-full`, `python3 -m pip install --user
  mlx-whisper` / `faster-whisper`, the DSH MiniMax bridge) and re-runs the check
  afterwards. The panel can only send an action id, the request must carry
  `confirm: true`, and no route in the plugin runs a shell — a scheduled round
  never installs anything. A whisper *model* deliberately gets no button: that is
  a multi-gigabyte download, so the panel says so and leaves it to you.
- **New: notifications can count any output as success.** `notify.successWhen`
  (`run` — the old behaviour, `anyOutput`) decides whether a partially failed
  round reports success, failure, or both. An `anyOutput` round that produced
  nothing and broke nothing stays silent instead of announcing an empty run.
- **New: a first-run checklist.** Opening the panel before a config file exists
  used to show one line saying the config was missing. It now lists five steps
  (dependencies, model, folders, a dry run, the optional schedule), ticks them
  against live state, and each unfinished step has a button that opens the tab it
  belongs to.
- The ledger and the failure list are written `0600`, like the host's own state
  files, instead of `0644`.

### Found by the independent review of this release

Six read-only reviewers were pointed at the diff (host routes, the ledger, the
selection rules, the test suites, the panel, the release set). Everything they
called a must-fix is in this release:

- **A night offline no longer means re-translating everything.** The first version
  of the ledger treated "could not list the folder" as "the product is gone", so an
  unreachable output endpoint threw away every record for that folder. A folder we
  could not look into now keeps its records and is reported as `unchecked`; only a
  folder we actually saw can veto a translation.
- **`{stem}_zh.{ext}` matched nothing.** The reverse matcher skipped the extension
  wildcard whenever the template ended in a placeholder, so rule (b) of `own-output`
  silently protected nothing in exactly the case it was written for. It also now
  refuses templates with unknown placeholders instead of failing later.
- **Name templates are trial-rendered at save time.** `{stem[:10]}` used to pass
  validation and then raise a `TypeError` per file during selection, killing the round.
- **`--only` and a forced rerun override `own-output`.** Naming a file is a request;
  the guard was meant to stop accidents.
- **A round that never started is no longer silent.** Failure while listing folders
  (dependency missing, endpoint unreachable, disk short) happened before `execute()`,
  so no notification went out at all — a scheduled run against a sleeping NAS said
  nothing. It now sends the failure notification (still silent when another instance
  holds the lock).
- **Read-only runs read only.** `plan` and `run --dry-run` could still write the
  failure list once (a key migration), which raced with a real run holding the lock.
- **Panel side**: an install whose dialog is unavailable no longer fails open — the
  card shows the exact command and asks inside the panel; the request timeout was
  raised above the server's own install timeout so a slow brew is not reported as a
  failure while it is still running; two installs cannot run at once (second gets
  `409`); install output folds this machine's home path to `~` and logs one line to
  the host log; on an Intel Mac the whisper button offers `faster-whisper` rather
  than a package that has no wheel there; a missing `selfcheck` verdict no longer
  renders a raw i18n key, and two different "faster" labels stopped colliding.

## v1.5.0

- **New: reasoning effort is a panel setting.** Models that declare thinking levels
  (the catalog never carries them — only `resolveModelInfo` does) now show a
  "Reasoning mode" picker next to the model it belongs to. Leaving it on "follow the
  model's default" omits the key entirely, which is what the host's own agent does;
  a model that declares nothing says so instead of being given an invented list.
  Under the hood the panel asks the host, the relay forwards the value, and the
  engine passes it to the translator.
- **New: the engine can tell a token-limit stop from a network error, and stops
  retrying it.** A thinking model given 8000 output tokens could spend them all on
  reasoning and get cut off mid-batch; the relay used to answer 502, the translator
  retried the same oversized batch three times, and every attempt was billed. The
  relay now answers 200 with `truncated: true` plus the text that did land and the
  token split (`usage.reasoningTokens`), and the translator treats a limit as
  terminal for that batch instead of re-asking for it.
- **New: a refused reasoning effort is answered before it is billed.** The
  relay checks the requested level against the ones the model declares and answers
  `422 unknown-effort` instead of starting a stream that dies on the first token;
  a provider that refuses the level mid-flight is recognised and answered the same
  way. The engine treats that as a configuration answer and stops retrying it,
  instead of asking the same doomed question three times.
- **New: an upstream rate limit is told apart from a broken stream.** A provider
  quota used to arrive as a plain `502`, so the engine backed off for a couple of
  seconds and tried again. It now comes back as `429 rate-limited`, which the
  engine retries with the long backoff, and the panel's "try this model" button
  reports it as what it is.
- **New: a cut-off batch keeps what it got.** Blocks that came back are matched by
  start timestamp and kept; the missing ones are re-asked in halves (depth and call
  caps apply), and only what still cannot be translated falls back to the source
  text. Each run writes `<output>.report.json` next to the subtitle file: calls,
  split retries, truncated and failed batches, token split, and the ids that stayed
  untranslated. The run result and the completion notification surface those ids, so
  "38% untranslated" is now a list you can act on instead of a number.

## v1.4.1

- **Fixed: the settings panel could disappear after re-activating the plugin.** The
  client registered its dictionary without handing the cleanup function back to the
  host, and the platform only allows one registration per namespace and language —
  a second activation in the same page (installing a bundle over HMR, toggling the
  plugin, a new module generation) threw `locale namespace "dsh-subtitle-studio"
  already has locale "zh"` before the panel slot was registered. The disposer is now
  returned, and a collision degrades to reusing the existing dictionary instead of
  taking the whole section down. Restarting DSH used to mask this; it is now fixed
  for good.
- **Fixed: ffmpeg was picked from PATH even when that build cannot burn subtitles.**
  Homebrew's plain `ffmpeg` formula no longer carries libass, and the formula that
  does (`ffmpeg-full`) is keg-only — installed under `<brew --prefix>/opt/ffmpeg-full/bin`,
  never linked into PATH. So a machine with ffmpeg-full correctly installed still
  got a binary without the `ass`/`subtitles` filters, and every burn failed. The
  engine now collects the PATH binary, the keg-only location (via `brew --prefix`
  and the two common prefixes) and uses the first one that actually has the filters;
  `ffprobe` follows the same directory. An explicitly configured `burn.ffmpeg` still
  wins outright and still fails loudly if missing — nothing is silently swapped. If
  no candidate can burn subtitles, the pipeline warns once on stderr and the
  diagnostics entry carries the exact install command.
- Diagnostics wording: `ffmpeg.filters` now says ffmpeg-full is searched for
  automatically, so the error means "no usable build on this machine".

## v1.4.0

- **Output mode** (`output.mode`: `burn` | `srt`) replaces the bare *burn in*
  switch: a job can now stop at translated subtitle files. `burn.enabled` follows
  the mode on save and still decides the outcome for files written by hand.
- **Timeline alignment**: when an audio track starts later than the video track,
  the gap used to shift every subtitle early. The measured offset is now padded
  back onto the audio before transcription.
- **Anti-hallucination controls** for transcription: no carry-over of previous
  text, temperature fallback ceiling, silence threshold, average-logprob and
  no-speech cut-offs, plus opt-out switches for end-card and repeated-line
  cleaning.
- **Subtitle style presets** (`subtitles.styleTemplate`): default, bold yellow,
  top white, boxed, cinema. One preset drives both the ASS writer and the
  `force_style` filter path.
- **Bilingual delivery follows the output mode** — choosing bilingual subtitles
  with `srt` output now ships the bilingual file, not the single-language one.
- Transcription engine is selectable in the panel (`transcribe.engine`).

## v1.3.0

- **Multiple source directories** (`dirs[]`): each entry carries its own source,
  output, include patterns and naming template; one job walks all of them.
- **Translation through the host**: `engine.kind: dsh` reuses the models already
  configured in DeepSeek Harness, so no second API key and no key on disk.
- Model picker reads the live host catalog; Ollama models sit in the same
  dropdown as host models.
- Per-directory work isolation, per-directory failure lists, and a plan that
  skips an unreachable directory instead of aborting the whole run.
- Scheduled runs support several guard times per day; the installed clock is
  written back to the config.
- Hardened the local relay: same-origin/Host gate, loopback-only endpoints,
  content-type and size limits, token and concurrency budgets, no full
  environment passthrough to the engine.

## v1.2.0

- First packaged release: scan → transcribe → translate → quality gate → burn in
  or sidecar, over local or SSH sources, with a launchd schedule and three
  notification channels.
