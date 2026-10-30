import struct
import zlib

import numpy as np
import pytest

from context_capture import (Attachment, Target, classify, compose, dib_to_pixels,
                             encode_png, is_ai_title)

# ── classify / is_ai_title ──


@pytest.mark.parametrize("app, title, kind", [
    ("chatgpt.exe", "", "chat"),
    ("Claude.exe", "Claude", "chat"),
    ("WindowsTerminal.exe", "claude", "terminal"),
    ("pwsh.exe", "", "terminal"),
    ("code.exe", "main.py - NoType - Visual Studio Code", "editor"),
    ("cursor.exe", "", "editor"),
    ("chrome.exe", "Neuer Chat - Claude - Google Chrome", "chat"),
    ("msedge.exe", "(2) ChatGPT - Microsoft Edge", "chat"),
    ("firefox.exe", "Google Gemini — Mozilla Firefox", "chat"),
    ("chrome.exe", "Wikipedia - Google Chrome", "browser"),
    ("applicationframehost.exe", "Microsoft Copilot", "chat"),
])
def test_classify(app, title, kind):
    target = classify(app, title)
    assert target is not None and target.kind == kind
    assert target.app == app.lower()


@pytest.mark.parametrize("app, title", [
    ("notepad.exe", "ChatGPT"),
    ("winword.exe", "Claude.docx"),
    ("applicationframehost.exe", "Einstellungen"),
    ("", ""),
    (None, None),
])
def test_classify_leaves_other_apps_alone(app, title):
    assert classify(app, title) is None


@pytest.mark.parametrize("title", [
    "ChatGPT",
    "Neuer Chat - Claude",
    "Google Gemini",
    "(3) ChatGPT",
    "● Claude",
    "Claude | Projekt",
    "Claude: neue Unterhaltung",
    "ChatGPT und 3 weitere Seiten - Profil 1 - Microsoft Edge",
    "Perplexity · Suche",
    "DeepSeek - Into the Unknown",
])
def test_ai_titles(title):
    assert is_ai_title(title)


@pytest.mark.parametrize("title", [
    "", None, "Meine Claude Notizen", "Wikipedia - Google Chrome",
    "ChatGPT-Alternativen im Test", "Gemini Sternzeichen Horoskop",
])
def test_non_ai_titles(title):
    assert not is_ai_title(title)


@pytest.mark.parametrize("kind, paths, early_text", [
    ("chat", False, True), ("browser", False, False),
    ("terminal", True, True), ("editor", True, False),
])
def test_target_properties(kind, paths, early_text):
    t = Target(kind, "x.exe")
    assert t.paths is paths
    assert t.early_text is early_text


# ── encode_png ──

def decode_png(data: bytes):
    """Minimal PNG reader for what encode_png writes (8 bit, filter 0/2)."""
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    pos, chunks = 8, []
    while pos < len(data):
        length = struct.unpack(">I", data[pos:pos + 4])[0]
        tag, body = data[pos + 4:pos + 8], data[pos + 8:pos + 8 + length]
        crc = struct.unpack(">I", data[pos + 8 + length:pos + 12 + length])[0]
        assert crc == zlib.crc32(tag + body)
        chunks.append((tag, body))
        pos += 12 + length
    assert [t for t, _ in chunks] == [b"IHDR", b"IDAT", b"IEND"]
    w, h, depth, ctype, comp, flt, interlace = struct.unpack(">IIBBBBB", chunks[0][1])
    assert (depth, comp, flt, interlace) == (8, 0, 0, 0)
    c = 4 if ctype == 6 else 3
    raw = np.frombuffer(zlib.decompress(chunks[1][1]), np.uint8).reshape(h, w * c + 1)
    rows = np.zeros((h, w * c), np.uint8)
    for y in range(h):
        f, line = raw[y, 0], raw[y, 1:]
        if f == 0:
            rows[y] = line
        elif f == 2:
            rows[y] = line + rows[y - 1]  # uint8 wraps like PNG
        else:
            raise AssertionError(f"unexpected filter {f}")
    return ctype, rows.reshape(h, w, c)


@pytest.mark.parametrize("channels, ctype", [(3, 2), (4, 6)])
def test_encode_png_roundtrip(channels, ctype):
    rng = np.random.default_rng(1)
    px = rng.integers(0, 256, (7, 5, channels), dtype=np.uint8)
    got_ctype, got = decode_png(encode_png(px))
    assert got_ctype == ctype
    assert np.array_equal(got, px)


def test_encode_png_single_row():
    px = np.array([[[1, 2, 3], [250, 251, 252]]], np.uint8)
    assert np.array_equal(decode_png(encode_png(px))[1], px)


def test_encode_png_flat_image_compresses():
    px = np.full((200, 300, 3), 200, np.uint8)
    assert len(encode_png(px)) < 2000


# ── dib_to_pixels ──

def make_dib(px_bgr, bpp=24, top_down=False, comp=0, masks=None, clr_used=0):
    h, w = px_bgr.shape[:2]
    stride = (w * bpp + 31) // 32 * 4
    rows = px_bgr if top_down else px_bgr[::-1]
    body = b"".join(r.tobytes().ljust(stride, b"\0") for r in rows)
    header = struct.pack("<IiiHHIIiiII", 40, w, -h if top_down else h, 1, bpp, comp,
                         len(body), 0, 0, clr_used, 0)
    extra = struct.pack("<III", *masks) if masks else b""
    return header + extra + b"\0" * (clr_used * 4) + body


RGB = np.array([[[255, 0, 0], [0, 255, 0], [0, 0, 255]],
                [[10, 20, 30], [40, 50, 60], [70, 80, 90]]], np.uint8)
BGR = RGB[..., ::-1]


def test_dib_24bit_bottom_up_with_row_padding():
    px = dib_to_pixels(make_dib(BGR))
    assert px.shape == (2, 3, 3)
    assert np.array_equal(px, RGB)


def test_dib_24bit_top_down():
    assert np.array_equal(dib_to_pixels(make_dib(BGR, top_down=True)), RGB)


def test_dib_32bit_zero_alpha_means_opaque_rgb():
    bgra = np.concatenate([BGR, np.zeros((2, 3, 1), np.uint8)], axis=2)
    px = dib_to_pixels(make_dib(bgra, bpp=32))
    assert px.shape == (2, 3, 3)
    assert np.array_equal(px, RGB)


def test_dib_32bit_full_alpha_gives_rgb():
    bgra = np.concatenate([BGR, np.full((2, 3, 1), 255, np.uint8)], axis=2)
    px = dib_to_pixels(make_dib(bgra, bpp=32))
    assert px.shape == (2, 3, 3)
    assert np.array_equal(px, RGB)


def test_dib_32bit_partial_alpha_gives_rgba():
    alpha = np.array([[[255], [128], [0]], [[1], [2], [3]]], np.uint8)
    px = dib_to_pixels(make_dib(np.concatenate([BGR, alpha], axis=2), bpp=32))
    assert px.shape == (2, 3, 4)
    assert np.array_equal(px[..., :3], RGB)
    assert np.array_equal(px[..., 3:], alpha)


def test_dib_bitfields_with_standard_masks():
    bgra = np.concatenate([BGR, np.zeros((2, 3, 1), np.uint8)], axis=2)
    dib = make_dib(bgra, bpp=32, comp=3, masks=(0xFF0000, 0xFF00, 0xFF))
    assert np.array_equal(dib_to_pixels(dib), RGB)


def test_dib_color_table_skipped():
    assert np.array_equal(dib_to_pixels(make_dib(BGR, clr_used=2)), RGB)


@pytest.mark.parametrize("dib", [
    None, b"", b"\0" * 39,
    make_dib(BGR)[:-1],                                         # truncated
    make_dib(np.zeros((2, 3, 2), np.uint8), bpp=16),            # 16 bit
    make_dib(BGR, comp=1),                                      # RLE
    make_dib(np.zeros((2, 3, 4), np.uint8), bpp=32, comp=3, masks=(0xF800, 0x7E0, 0x1F)),
])
def test_dib_unsupported_layouts(dib):
    assert dib_to_pixels(dib) is None


def test_dib_zero_width_rejected():
    header = struct.pack("<IiiHHIIiiII", 40, 0, 2, 1, 24, 0, 0, 0, 0, 0, 0)
    assert dib_to_pixels(header + b"\0" * 16) is None


def test_dib_to_png_roundtrip():
    assert np.array_equal(decode_png(encode_png(dib_to_pixels(make_dib(BGR))))[1], RGB)


# ── compose ──

CHAT, TERMINAL = Target("chat", "chatgpt.exe"), Target("terminal", "wt.exe")


def text_item(text, path=""):
    return Attachment("text", 1, "text", text=text, path=path)


def test_compose_without_items():
    assert compose("Hallo", [], CHAT) == "Hallo"
    assert compose("Hallo", [], TERMINAL) == "Hallo"


def test_compose_chat_quotes_text_below():
    assert compose("Was heißt das?", [text_item("Error: boom")], CHAT) == "Was heißt das?\n\n> Error: boom"


def test_compose_chat_quotes_every_line_and_blank_lines():
    assert compose("Erklär", [text_item("a\n\nb\n")], CHAT) == "Erklär\n\n> a\n>\n> b"


def test_compose_chat_images_not_in_text():
    img = Attachment("image", 2, "screenshot", formats={1: b"x"})
    assert compose("Schau", [img], CHAT) == "Schau"


def test_compose_terminal_paths_in_front():
    img = Attachment("image", 2, "screenshot", path=r"C:\att\a.png")
    assert compose("fix this", [img], TERMINAL) == r"C:\att\a.png fix this"


def test_compose_terminal_quotes_paths_with_spaces():
    img = Attachment("image", 2, "image", path=r"C:\Users\Max Muster\a.png")
    assert compose("fix", [img], TERMINAL) == r'"C:\Users\Max Muster\a.png" fix'


def test_compose_terminal_short_text_inline():
    assert compose("explain", [text_item("  KeyError: 'x'  ")], TERMINAL) == "explain \"KeyError: 'x'\""


def test_compose_terminal_long_text_as_path():
    item = text_item("line1\nline2", path=r"C:\att\t.txt")
    assert compose("read", [item], TERMINAL) == r"C:\att\t.txt read"


def test_attachment_info():
    info = text_item("eins\nzwei   drei").info()
    assert info == {"kind": "text", "preview": "eins zwei drei", "lines": 2}
    img = Attachment("image", 1, "screenshot", thumb="data:x").info()
    assert img == {"kind": "image", "source": "screenshot", "thumb": "data:x"}
