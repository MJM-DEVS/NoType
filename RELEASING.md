# Releasing NoType

> [!NOTE]
> This checklist was written from `electron/package.json`, `backend.spec`, `electron/main.js`, the README and the git history. It has not been run step by step as written. Where it says *derived*, check the step against the previous release on GitHub before relying on it.

Releases are built by hand on a Windows 11 x64 machine. There is no release CI.

## 1. Prepare

- Make sure `main` contains everything for the release and the checks in [docs/development.md](docs/development.md#what-you-can-test-on-linux-or-macos) pass.
- Move the *Unreleased* entries in `CHANGELOG.md` under a new version heading with today's date.
- Bump the version in its own commit, as in the history ("Bump version to x.y.z"). The version lives only in `electron/package.json` and `electron/package-lock.json`; this updates both without creating a tag:

  ```bat
  cd electron
  npm version 3.4.0 --no-git-tag-version
  ```

  The app reads its version from there (`app.getVersion()`), including the `User-Agent` of agent requests and the update check.

## 2. Build the backend

```bat
:: repository root, venv active
pip install -r requirements-build.txt
pyinstaller --noconfirm backend.spec
```

This writes `dist\backend\backend.exe` with an `_internal\` folder. `backend.spec` stops if the cuBLAS DLLs from `nvidia-cublas-cu12` are missing, and generates the comtypes UI Automation wrappers, so it has to run on Windows.

Check that `dist\backend\backend.exe` exists before the next step. electron-builder copies `..\dist\backend` into `resources\backend\` (`extraResources`); without it the app has no backend.

## 3. Build the installer and the portable ZIP

```bat
cd electron
npm install
npm run build
```

Output in `dist\`:

| File | What it is |
|---|---|
| `NoType-Setup-<version>.exe` | NSIS installer (`nsis.artifactName`) |
| `NoType-Setup-<version>.exe.blockmap` | lets electron-updater download only changed blocks |
| `latest.yml` | update feed: version, file name, size and SHA-512 of the installer |
| `NoType-<version>-portable.zip` | portable ZIP of `win-unpacked\` (`zip` target, `win.artifactName`) |
| `win-unpacked\` | the unpacked app the ZIP is made from |

`latest.yml` must name only the installer, not the ZIP. `npm install` is needed for `@electron/fuses`, which the afterPack hook (`electron/build-resources/fuses.js`) uses; without it the build stops.

> [!WARNING]
> **About `--publish`.** `package.json` has a GitHub publish target with `"releaseType": "release"`. Without a `--publish` flag, electron-builder decides by itself whether to upload, depending on the environment (for example a detected CI and a `GH_TOKEN`). That is why `npm run build` and `npm run build:dir` pass `--publish never`; don't call `electron-builder` directly without it. With `--publish always` and a token, electron-builder would upload to a GitHub release for the tag and, because of `releaseType: release`, not as a draft. Every installed copy would then be offered that version on its next check. (*Derived* from the configuration and electron-builder's documented behavior; not tried.)

## 4. Check the portable ZIP

Before the first release built this way, compare the folder layout inside `NoType-<version>-portable.zip` with the ZIP of the previous release. `NoType.exe` should sit at the top level.

How the app tells the two apart (`electron/main.js`): it updates itself only when `Uninstall NoType.exe` sits next to `NoType.exe`. `win-unpacked` has no uninstaller, so the portable version only links to the release page. Don't add one to the ZIP.

## 5. Try it

On a Windows 11 PC, ideally without Python or a CUDA toolkit on `PATH`:

- Install over the previous version; settings and dictionary stay.
- First dictation works; on an NVIDIA card the backend reports CUDA, not CPU.
- Start the portable ZIP from another folder.

## 6. Tag and publish

```bat
git tag v3.4.0
git push origin v3.4.0
```

The tag has to be `v<version>`; electron-builder's GitHub provider looks for `v`-prefixed tags. (The history has no `v3.0.0` tag; tag every release.)

Create the GitHub release for that tag as a normal release (not a pre-release) and upload:

- `NoType-Setup-<version>.exe`
- `NoType-Setup-<version>.exe.blockmap`
- `latest.yml`
- `NoType-<version>-portable.zip`

Paste the `CHANGELOG.md` section as release notes.

## How installed copies find the update

The installer ships `resources\app-update.yml`, which electron-builder writes from the `publish` block. 20 seconds after start and every 6 hours (unless switched off), `electron-updater` asks GitHub for the newest **non-pre-release** release of `MJM-DEVS/NoType` and reads its `latest.yml`. If the version there is higher, NoType offers it; after a click it downloads the file named in `latest.yml`, checks its SHA-512, runs it silently and starts NoType again.

So:

- **`latest.yml` must be attached**, or nobody gets the update.
- Its file name and hash must match the uploaded installer. Don't rename the installer, and upload the `latest.yml` from the same build.
- Publishing a release is what ships it. Before upload, everything can still change; after, assume it is out.
- The installers are not code-signed. Windows SmartScreen warns on first start, and the updater has no publisher check.

## Afterwards

- Start a new *Unreleased* section in `CHANGELOG.md`.
- `SECURITY.md` and the issue templates say "latest release" instead of a version number; if you added a number anywhere, update it.
- If the README's download section or screenshots changed, check that its links still point at the right assets.
