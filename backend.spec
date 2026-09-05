# PyInstaller spec for the NoType Python backend.
#
# Usage (from the project root, with the venv activated):
#   pyinstaller --noconfirm backend.spec
#
# Output: dist/backend/backend.exe (and a _internal/ folder with native libs).
# electron-builder will pull this whole folder into resources/backend/ at packaging time.
#
# We use --onedir (default) rather than --onefile because faster-whisper +
# ctranslate2 ship large native DLLs/CUDA libs; onefile would extract to %TEMP%
# on every launch (slow + race conditions). onedir starts instantly.

# -*- mode: python ; coding: utf-8 -*-

from PyInstaller.utils.hooks import collect_all

# faster_whisper ships a Silero VAD .onnx in its `assets/` folder. ctranslate2
# ships native DLLs. Use collect_all to pull modules + data + binaries for both,
# so onnxruntime can find silero_vad_v6.onnx at runtime.
fw_datas, fw_binaries, fw_hidden = collect_all('faster_whisper')
ct_datas, ct_binaries, ct_hidden = collect_all('ctranslate2')

# CUDA runtime libraries. The ctranslate2 wheel ships cuDNN but NOT cuBLAS –
# ctranslate2.dll imports cublas64_12.dll (which pulls cublasLt64_12.dll) at
# load time. Without them the GPU path fails on every machine that doesn't
# happen to have a CUDA 12 toolkit on PATH, and the backend silently falls
# back to CPU. Source: the `nvidia-cublas-cu12` pip package in the venv.
# Placed next to cudnn64_9.dll inside ctranslate2/ – the directory the
# package registers with os.add_dll_directory, so the loader finds them.
import glob, os, sysconfig
_site = sysconfig.get_paths()['purelib']
cuda_binaries = [
    (p, 'ctranslate2')
    for p in glob.glob(os.path.join(_site, 'nvidia', 'cublas', 'bin', 'cublas*64_12.dll'))
]
if not cuda_binaries:
    raise SystemExit(
        "cuBLAS DLLs not found – run: venv\\Scripts\\pip install nvidia-cublas-cu12 "
        "(the GPU path needs them bundled, see comment in backend.spec)"
    )

block_cipher = None

a = Analysis(
    ['backend.py'],
    pathex=[],
    binaries=fw_binaries + ct_binaries + cuda_binaries,
    datas=fw_datas + ct_datas,
    hiddenimports=[
        'sounddevice',
        'soundfile',
        'numpy',
    ] + fw_hidden + ct_hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'tkinter',
        'matplotlib',
        'pytest',
        'IPython',
        'jupyter',
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='backend',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,             # UPX corrupts some onnx/ctranslate2 DLLs
    # console=False = Windows GUI subsystem. No console window EVER, regardless
    # of how the exe is launched (Electron spawn, Win11 restart-apps, direct
    # double-click). Python's stdin/stdout would normally be None in this mode;
    # backend.py recovers them via os.fdopen(0/1/2) when launched as a child of
    # a parent that pipes those file descriptors (which Electron's spawn does).
    console=False,
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='backend',
)
