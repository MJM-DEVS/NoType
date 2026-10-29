## What and why

<!-- What changes for users, and why. Link the issue if there is one. -->

## How it was tested

<!-- Be specific. Windows-only parts (audio, hotkeys, text insertion, UI Automation, Electron UI, installer) can't be tested on Linux; say so if you couldn't run them on Windows. -->

- [ ] `pytest`
- [ ] `ruff check --select E9,F .`
- [ ] `python tools/gen_config_docs.py --check`
- [ ] `node --check` on changed JavaScript files
- [ ] Tried on Windows 11 (describe below)
- [ ] Not tested on Windows

## Checklist

- [ ] No dictated text, selection, clipboard content, token or webhook URL is logged or sent anywhere new
- [ ] New user-facing strings exist in German and English
- [ ] New settings keys are in `DEFAULT_CONFIG` and `docs/configuration.md` is regenerated
- [ ] README and `CHANGELOG.md` (*Unreleased*) updated if behavior changed

By opening this pull request I agree that my contribution is licensed under the project's [license](https://github.com/MJM-DEVS/NoType/blob/main/LICENSE) (MIT with the Commons Clause).
