# Security policy

## Reporting a vulnerability

Please report security problems privately, not in a public issue.

Use the **Report a vulnerability** button under the **Security** tab of the repository when it is available; otherwise open an issue that asks for a private contact without including details.

Please include the NoType version (Settings → General, or the installer file name), whether you use the installer or the portable ZIP, and the steps to reproduce. If you attach `%APPDATA%\NoType\notype.log`, check it first; it holds no dictated text, but it can hold program names, model names, the host names of agent requests and file paths that contain your Windows user name.

NoType is a free project maintained by one person. There is no bug bounty and no guaranteed response time.

## Supported versions

Only the latest release gets fixes. The installed version updates itself (see [Updates](#updates)); the portable ZIP has to be replaced by hand. The current version number is on the [releases page](https://github.com/MJM-DEVS/NoType/releases/latest).

| Version | Supported |
|---|---|
| latest release | yes |
| anything older | no, please update |

## Threat model

NoType runs as the signed-in user on a single Windows PC. It is built to keep dictated text on that PC unless the user turns on a feature that sends it somewhere. The sections below describe what the code does today, so reports can be measured against it.

### Out of scope

- Anything that needs code already running as the same Windows user, or as an administrator. Such code can read `settings.json`, the clipboard and the screen anyway.
- Other software on the PC that listens on `127.0.0.1:11434` (see [Ollama](#ollama)).
- What happens to a dictation at the URL the user configured for the agent hotkey.
- Vulnerabilities in Windows, Electron/Chromium, Python, faster-whisper, CTranslate2 or Ollama themselves, unless NoType uses them in an unsafe way.
- SmartScreen warnings: the releases are not code-signed (see [Updates](#updates)).

### Processes and IPC

The Electron app starts the Python backend (`backend.exe`) as a child process. They talk newline-delimited JSON over stdin/stdout. The app and the backend open no listening network port themselves; the Ollama server that NoType can start does (see [Ollama](#ollama)). All Electron windows run with `contextIsolation: true` and `nodeIntegration: false` and reach the main process through `electron/preload.js`.

### Agent hotkey and its token

Off by default (`agent_enabled`). When it is on, `agent_webhook.py` sends each agent dictation with an HTTP `POST` to `agent_url`:

- The body holds the text, its language, a timestamp and, while `agent_send_context` is on, the program name and window title of the foreground window.
- `agent_token`, if set, goes out as `Authorization: Bearer <token>`. It is stored in plain text in `settings.json`.
- With an `http://` URL the text travels unencrypted, and so does the token whenever it is sent over `http://`. Whether the token is also sent over `http://` to public hosts depends on the version; see `agent_webhook.py`. Use `https://` for anything that is not on your own PC or network.
- Redirects are not followed, so the token is not handed to another host. The answer is read up to 64 KB and shown as one line of at most 200 characters.
- The log records host, status code, timing and answer size; never the URL path or query, the token, the text or the answer.

### Clipboard

- Text is inserted with <kbd>Ctrl</kbd>+<kbd>V</kbd> through the clipboard (`text_output.py`) unless `insert_method` is `type`. The dictated text stays on the clipboard afterwards and therefore also in Windows clipboard history, if it is on. With Windows clipboard sync across devices on, Windows can upload it to the user's Microsoft account and other devices; NoType has no control over that.
- Command mode copies the current selection to read it.
- With `ai_context` on, `context_capture.py` reads copies made from one minute before until the end of a dictation, and only when the foreground app is an AI app, a terminal, a code editor or a browser. Copies for terminals and editors are saved as files under `%APPDATA%\NoType\attachments\` and removed by a cleanup once they are older than 24 hours. When that cleanup runs depends on the version (see `context_capture.py`), so files can stay longer than 24 hours.

### UI Automation (correction learning)

With `learn_corrections` on, `correction_watch.py` reads the text field the user just dictated into through Windows UI Automation for up to a minute, and compares only the dictated part. It skips fields that report `IsPassword`. Nothing is stored unless the user clicks **Learn**. Fields that do not report themselves as password fields are read like any other.

### Keyboard hook

`escape_hook.py` installs a low-level keyboard hook only while a dictation records or is processed, and only acts on <kbd>Esc</kbd>.

### Updates

The installed version uses `electron-updater` with the GitHub provider: 20 seconds after start and every 6 hours (unless `update_check` is off) it reads `latest.yml` from the newest release of `MJM-DEVS/NoType`. Nothing is downloaded before the user clicks **Update now**; the installer then runs silently.

- `electron-updater` checks the downloaded installer against the SHA-512 in `latest.yml`. Both come from the same GitHub release.
- The installers are not code-signed and no publisher check is configured. The trust anchor is HTTPS and the GitHub account that can publish releases: whoever can publish a release there can ship code to every installed copy.
- The portable ZIP does not update itself, but it runs the same check against GitHub (unless `update_check` is off) and, when a newer version exists, links to the release page. Dev runs (`npm start`) do not check.

### Ollama

AI cleanup and command mode send the dictated text (and, for command mode, the selection) to an Ollama API at `http://127.0.0.1:11434`. If something already answers there, NoType uses it instead of starting its own copy.

When NoType starts its own `ollama serve`, that server listens on `127.0.0.1:11434` (or on whatever an `OLLAMA_HOST` environment variable says) without authentication, like any Ollama. While it runs, other programs of any user on the PC can use its API, including the models NoType pulled. NoType stops it together with the backend.

On request, `ollama_manager.py` downloads the portable Ollama for Windows over HTTPS from `github.com/ollama/ollama/releases` and unpacks it into `%APPDATA%\NoType\ollama\`. The in-app Ollama update asks the GitHub API for the newest version and downloads it the same way. Whether the ZIP is checked against a published checksum depends on the version; see `ollama_manager.py`. Either way the trust anchor is HTTPS and Ollama's GitHub releases; there is no signature check. Language models are pulled by Ollama from its own registry.

### Speech models

faster-whisper downloads the chosen model from the Hugging Face Hub into the shared cache `%USERPROFILE%\.cache\huggingface\hub\`. A model that is already there is loaded from disk without contacting the Hub.

### Local data

Everything else lives in `%APPDATA%\NoType\` with the user's normal file permissions: `settings.json` (including `agent_token`) and its backups, `history.json` (the last 10 dictations in plain text), `notype.log`, `attachments\` and `ollama\`. The uninstaller removes this folder.
