# Contributing to NoType

Bug reports, ideas and pull requests are welcome.

## Reporting bugs and ideas

[Open an issue](https://github.com/MJM-DEVS/NoType/issues/new/choose) and pick the bug report or feature request form. For a bug, the most useful things are:

- the NoType version (Settings → General) and whether you use the installer or the portable ZIP,
- your graphics card (or "none") and the speech model,
- `%APPDATA%\NoType\notype.log`. It contains timings and errors, never your dictated text. It can contain program names and model names, so have a look before you post it.

Security problems do not belong in a public issue; see [SECURITY.md](SECURITY.md).

## Development setup

How to run NoType from source, build the installer and test the platform-neutral parts on Linux is in [docs/development.md](docs/development.md). In short:

- NoType is a Windows 11 app. Audio, hotkeys, text insertion, UI Automation, the Electron UI and the installer can only be tested on Windows.
- `config.py`, `dictionary.py`, `spoken_punctuation.py`, `hallucinations.py`, `postprocess.py` and `agent_webhook.py` import on any OS with only numpy installed, and `pytest` runs on Linux.
- Before a pull request, run `pytest`, `ruff check --select E9,F .`, `python tools/gen_config_docs.py --check` and `node --check` on the JavaScript files you touched.

## Ground rules

- **Privacy first.** No network traffic unless a feature needs it and the user started it or turned it on. Never write dictated text, selections, clipboard content, agent tokens or webhook URLs to the log; lengths, timings and host names are fine.
- **Both UI languages.** User-facing strings exist in German and English; add both.
- **Small, focused changes.** One topic per pull request; no drive-by refactoring.
- **Say what you tested.** If you could not test a Windows-only change on Windows, write that in the pull request.
- **Settings.** A new key goes into `DEFAULT_CONFIG` in `config.py` with a short comment; then regenerate `docs/configuration.md`.
- **Docs.** Update the README when user-visible behavior changes, and add a line to `CHANGELOG.md` under *Unreleased*. README text uses no em dashes; write number ranges as "2 to 5".

## Commit style

Look at `git log` for examples. Commits use:

- a short subject in the imperative, written for users where possible ("Add agent hotkey that sends dictation to a URL", "Fix settings not saving when the backend is down or restarting"), no prefix, no trailing period;
- an optional body that says what changes for the user and why, wrapped at about 72 characters;
- version bumps as their own commit: "Bump version to x.y.z".

## License

NoType is licensed under the [MIT License with the Commons Clause](LICENSE): free to use, modify and share, but not to sell. It is source-available, not OSI open source. By submitting a pull request you agree that your contribution is licensed under the same terms.
