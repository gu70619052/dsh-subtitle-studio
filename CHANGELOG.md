# Changelog

All dates are the day the release was tagged. Config keys added by a release keep
working on older files: everything is deep-merged over defaults, so an upgrade
never needs a migration step.

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
