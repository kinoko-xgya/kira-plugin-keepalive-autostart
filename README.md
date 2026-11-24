# kira-plugin-keepalive-autostart

KiraAI v3 plugin. Starts the `keepalive` supervisor when the container boots, and
keeps an eye on it afterwards.

## Why

The container image is just `python main.py` — no cron, no systemd. Long-running
scripts are kept alive by `keepalive.py`, but nothing watched the watcher: after a
power cut on 2026-11-09 the container came back and keepalive did not, leaving two
supervised daemons dead for two hours. `ON_LOADED` fires on every container start,
so that is the natural hook.

## What it does

1. On load (and on `ON_LOADED`), scan `/proc` for a running `keepalive.py`.
   If present, do nothing — the plugin stays idempotent across hot reloads.
2. Otherwise spawn it with `start_new_session=True` so a framework restart does not
   take it down, then poll for up to 3 s to confirm a real pid exists.
3. Optionally run one `keepalive.py --once` pass so already-dead children recover
   immediately instead of waiting a full interval.
4. Every `watch_interval` seconds, check again and respawn if it disappeared.

Failures are logged to `<scripts_dir>/keepalive_autostart.log` and never raised —
a broken supervisor must not prevent the plugin (or the bot) from loading.

## Config

| key | type | default | meaning |
|---|---|---|---|
| `enabled` | switch | true | master switch |
| `scripts_dir` | string | `/app/data/files/scripts` | where `keepalive.py` lives |
| `python_bin` | string | `python3` | interpreter used to launch it |
| `startup_delay` | integer | 10 | seconds to wait after load, `0` = immediate |
| `run_once_after_start` | switch | true | run `--once` after starting |
| `watch_interval` | integer | 600 | watchdog period, `0` disables |

## Requirements

KiraAI v3.0.0. No third-party dependencies. Needs `/proc` (Linux) and a local
`keepalive.py` — this plugin is a launcher for that script, not a replacement.

## License

MIT

## Changelog

- **1.1.0** — English comments and log lines; config defaults now read via
  `None` checks so `0` is honoured; watchdog loop nested in `try` so a single error
  does not silently end it; `terminate()` made re-entrant.
- **1.0.0** — Initial release: boot-time autostart, optional `--once` pass,
  process-presence watchdog.
