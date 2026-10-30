// electron-builder afterPack hook: switch off Electron features NoType never
// uses, so NoType.exe can't be borrowed as a general Node.js runtime.
//   RunAsNode                          ELECTRON_RUN_AS_NODE=1 NoType.exe script.js
//   EnableNodeOptionsEnvironmentVariable  code injected via NODE_OPTIONS
//   EnableNodeCliInspectArguments      --inspect / --inspect-brk debugger
// Nothing in main.js needs them: the backend is spawned as its own exe and
// there is no child_process.fork or ELECTRON_RUN_AS_NODE. electron-updater
// starts the downloaded NSIS installer itself.
// asar stays off ("asar": false in package.json), so OnlyLoadAppFromAsar
// stays untouched and every other fuse keeps Electron's default. That also
// limits what this protects: whoever can write to the (per-user) install
// folder can still edit resources\app\main.js directly.
const path = require('path');
const { flipFuses, FuseVersion, FuseV1Options } = require('@electron/fuses');

exports.default = async function flipNoTypeFuses(context) {
  if (context.electronPlatformName !== 'win32') return;
  const exe = path.join(context.appOutDir, `${context.packager.appInfo.productFilename}.exe`);
  await flipFuses(exe, {
    version: FuseVersion.V1,
    [FuseV1Options.RunAsNode]: false,
    [FuseV1Options.EnableNodeOptionsEnvironmentVariable]: false,
    [FuseV1Options.EnableNodeCliInspectArguments]: false,
  });
};
