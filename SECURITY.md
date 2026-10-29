# Security policy

## Reporting a vulnerability

Please report security problems privately, not in a public issue.

The intended way is GitHub's private vulnerability reporting: **Security → Report a vulnerability** on the repository page. This only works once the maintainer has turned it on in the repository settings. If the button is not there, open a public issue that only asks for a private contact, without any details of the problem.

Please include the NoType version (Settings → General, or the installer file name), whether you use the installer or the portable ZIP, and the steps to reproduce. If you attach `%APPDATA%\NoType\notype.log`, check it first; it holds no dictated text, but it can hold program names, model names and the host names of agent requests.

NoType is a free project maintained by one person. There is no bug bounty and no guaranteed response time.

## Supported versions

Only the latest release gets fixes. The installed version updates itself (see [Updates](#updates)); the portable ZIP has to be replaced by hand.

| Version | Supported |
|---|---|
| latest release (3.3.x) | yes |
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

The Electron app starts the Python backend (`backend.exe`) as a child process. They talk newline-delimited JSON over stdin/stdout. NoType itself opens no listening network port. All Electron windows run with `contextIsolation: true` and `nodeIntegration: false` and reach the main process through `electron/preload.js`.

### Agent hotkey and its token

Off by default (`agent_enabled`). When it is on, `agent_webhook.py` sends each agent dictation with an HTTP `POST` to `agent_url`:

- The body holds the text, its language, a timestamp and, while `agent_send_context` is on, the program name and window title of the foreground window.
- `agent_token`, if set, goes out as `Authorization: Bearer <token>`. It is stored in plain text in `settings.json`.
- Both `http://` and `https://` URLs are accepted. With `http://` the text and the token travel unencrypted.
- Redirects are not followed, so the token is not handed to another host. The answer is read up to 64 KB and shown as one line of at most 200 characters.
- The log records host, status code, timing and answer size; never the URL path or query, the token, the text or the answer.

### Clipboard

- Text is inserted with <kbd>Ctrl</kbd>+<kbd>V</kbd> through the clipboard (`text_output.py`) unless `insert_method` is `type`. The dictated text stays on the clipboard afterwards and therefore also in Windows clipboard history or cloud clipboard sync, if those are on.
- Command mode copies the current selection to read it.
- With `ai_context` on, `context_capture.py` reads copies made from one minute before until the end of a dictation, and only when the foreground app is an AI app, a terminal, a code editor or a browser. Copies for terminals and editors are saved as files under `%APPDATA%\NoType\attachments\` and deleted after 24 hours.

### UI Automation (correction learning)

With `learn_corrections` on, `correction_watch.py` reads the text field the user just dictated into through Windows UI Automation for up to a minute, and compares only the dictated part. It skips fields that report `IsPassword`. Nothing is stored unless the user clicks **Learn**. Fields that do not report themselves as password fields are read like any other.

### Keyboard hook

`escape_hook.py` installs a low-level keyboard hook only while a dictation records or is processed, and only acts on <kbd>Esc</kbd>.

### Updates

The installed version uses `electron-updater` with the GitHub provider: 20 seconds after start and every 6 hours (unless `update_check` is off) it reads `latest.yml` from the newest release of `MJM-DEVS/NoType`. Nothing is downloaded before the user clicks **Update now**; the installer then runs silently.

- `electron-updater` checks the downloaded installer against the SHA-512 in `latest.yml`. Both come from the same GitHub release.
- The installers are not code-signed and no publisher check is configured. The trust anchor is HTTPS and the GitHub account that can publish releases: whoever can publish a release there can ship code to every installed copy.
- The portable ZIP and dev runs do not update themselves; they link to the release page.

### Ollama

AI cleanup and command mode send the dictated text (and, for command mode, the selection) to an Ollama API at `http://127.0.0.1:11434`. If something already answers there, NoType uses it instead of starting its own copy.

On request, `ollama_manager.py` downloads the portable Ollama for Windows over HTTPS from `github.com/ollama/ollama/releases` and unpacks it into `%APPDATA%\NoType\ollama\`. NoType does not verify a checksum or signature of that ZIP; it relies on HTTPS and GitHub. The same goes for the in-app Ollama update, which also asks the GitHub API for the newest version. Language models are pulled by Ollama from its own registry.

### Speech models

faster-whisper downloads the chosen model from the Hugging Face Hub into the shared cache `%USERPROFILE%\.cache\huggingface\hub\`. A model that is already there is loaded from disk without contacting the Hub.

### Local data

Everything else lives in `%APPDATA%\NoType\` with the user's normal file permissions: `settings.json` (including `agent_token`) and its backups, `history.json` (the last 10 dictations in plain text), `notype.log`, `attachments\` and `ollama\`. The uninstaller removes this folder.
