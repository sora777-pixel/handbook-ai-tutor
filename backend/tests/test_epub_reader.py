"""EPUB reader endpoints: chapter list, sanitised rendering, image proxy.

Two things matter here beyond "it returns 200":

* an uploaded book is untrusted input rendered in the app's own origin, so the
  sanitiser must actually remove script/handlers/remote beacons;
* the asset proxy must not become a file-read primitive (`../../`, non-image
  extensions).
"""

from __future__ import annotations

import zipfile
from io import BytesIO

import pytest

from app.api.sources_epub import CSP, _rewrite
from app.services.epub_reader import open_epub

CHAPTER = """<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml"><head><title>c1</title>
<script>window.stealCookies()</script>
<style>@import url(http://evil.example/x.css);</style>
</head>
<body onload="alert(1)">
<h1 onclick="alert(2)">第一章 起风</h1>
<p>他把书翻开，<b>风</b>从纸页间穿过去。</p>
<p><a href="ch2.xhtml">下一章</a> · <a href="javascript:alert(3)">坏链接</a></p>
<p><img src="images/fig.png" alt="插图"/>
   <img src="https://tracker.example/pixel.gif" alt="追踪"/></p>
</body></html>
"""

CHAPTER2 = """<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml"><head><title>c2</title></head>
<body><h1>第二章 落雨</h1><p>雨点砸在窗台上。</p></body></html>
"""

CONTAINER = """<?xml version="1.0"?>
<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container" version="1.0">
<rootfiles><rootfile full-path="OEBPS/content.opf"
  media-type="application/oebps-package+xml"/></rootfiles></container>
"""

OPF = """<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="uid">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:title>风与雨</dc:title><dc:creator>林一</dc:creator>
  </metadata>
  <manifest>
    <item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>
    <item id="c1" href="ch1.xhtml" media-type="application/xhtml+xml"/>
    <item id="c2" href="ch2.xhtml" media-type="application/xhtml+xml"/>
    <item id="img" href="images/fig.png" media-type="image/png"/>
  </manifest>
  <spine><itemref idref="c1"/><itemref idref="c2"/></spine>
</package>
"""

NAV = """<?xml version="1.0" encoding="utf-8"?>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">
<body><nav epub:type="toc"><ol>
<li><a href="ch1.xhtml">第一章 起风</a></li>
<li><a href="ch2.xhtml">第二章 落雨</a></li>
</ol></nav></body></html>
"""

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 2048


def build() -> bytes:
    buf = BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("mimetype", "application/epub+zip")
        zf.writestr("META-INF/container.xml", CONTAINER)
        zf.writestr("OEBPS/content.opf", OPF)
        zf.writestr("OEBPS/nav.xhtml", NAV)
        zf.writestr("OEBPS/ch1.xhtml", CHAPTER)
        zf.writestr("OEBPS/ch2.xhtml", CHAPTER2)
        zf.writestr("OEBPS/images/fig.png", PNG)
    return buf.getvalue()


# ------------------------------------------------------------------ parsing


def test_open_epub_lists_chapters_in_spine_order():
    book = open_epub(build())
    assert book.title == "风与雨"
    assert book.authors == ["林一"]
    assert [c.title for c in book.chapters] == ["第一章 起风", "第二章 落雨"]
    assert [c.index for c in book.chapters] == [0, 1]
    assert "风" in book.chapters[0].text


# --------------------------------------------------------------- sanitising


def test_rewrite_removes_active_content_and_reproxies_assets():
    out = _rewrite(CHAPTER, "OEBPS/ch1.xhtml", "abc", {"OEBPS/ch2.xhtml": 1})

    assert "<script" not in out.lower()
    assert "window.stealCookies" not in out
    assert "onload" not in out.lower()
    assert "onclick" not in out.lower()
    assert "@import" not in out            # remote CSS beacon
    assert "tracker.example" not in out    # remote pixel dropped
    assert "javascript:" not in out.lower()

    # internal links and images now point at this API, relative to the chapter
    assert "/api/v1/sources/abc/epub/asset?path=OEBPS/images/fig.png" in out
    # an in-book link becomes reader navigation, not an image proxy
    assert 'href="/api/v1/sources/abc/epub/chapter?i=1"' in out


def test_rewrite_resolves_relative_asset_paths():
    html = '<html><body><img src="../images/a.png"/></body></html>'
    out = _rewrite(html, "OEBPS/text/ch1.xhtml", "abc")
    # "../images/a.png" relative to OEBPS/text/ resolves back to OEBPS/images/a.png
    assert "path=OEBPS/images/a.png" in out


def test_csp_forbids_scripts_and_remote_images():
    assert "script-src 'none'" in CSP
    assert "img-src 'self'" in CSP
    assert "http://" not in CSP.replace("http://www.w3.org", "")


# --------------------------------------------------------------- asset proxy


@pytest.mark.parametrize(
    "path",
    ["../secret.txt", "OEBPS/../../secrets.env", "images/../../x.png", "/etc/passwd"],
)
def test_traversal_paths_are_rejected(path):
    from fastapi import HTTPException

    from app.api.sources_epub import resolve_asset_path

    with pytest.raises(HTTPException) as caught:
        resolve_asset_path(path)
    assert caught.value.status_code in (400, 415)


def test_valid_asset_path_resolves():
    from app.api.sources_epub import resolve_asset_path

    assert resolve_asset_path("OEBPS/images/fig.png") == "OEBPS/images/fig.png"


def test_markup_and_opf_cannot_be_read_through_the_image_proxy():
    from fastapi import HTTPException

    from app.api.sources_epub import resolve_asset_path

    for path in ("OEBPS/content.opf", "OEBPS/ch1.xhtml", "META-INF/container.xml"):
        with pytest.raises(HTTPException) as caught:
            resolve_asset_path(path)
        assert caught.value.status_code == 415


def test_only_raster_suffixes_allowed():
    from app.api.sources_epub import _ASSET_SUFFIXES

    assert ".png" in _ASSET_SUFFIXES
    assert ".svg" not in _ASSET_SUFFIXES  # svg can carry script
    assert ".xhtml" not in _ASSET_SUFFIXES

# ------------------------------------------------- inline reader (json mode)


def test_anchor_link_mode_targets_the_inline_reader():
    out = _rewrite(
        CHAPTER, "OEBPS/ch1.xhtml", "abc", {"OEBPS/ch2.xhtml": 1}, link_mode="anchor"
    )
    # the inline reader intercepts fragments, so a chapter link must not become
    # an API URL that would navigate the tab away from the book
    assert 'href="#epub-chapter-1"' in out
    assert "/epub/chapter?i=" not in out
    # images still resolve through the asset proxy
    assert "/epub/asset?path=" in out


def test_page_link_mode_keeps_api_urls_for_standalone_documents():
    out = _rewrite(CHAPTER, "OEBPS/ch1.xhtml", "abc", {"OEBPS/ch2.xhtml": 1})
    assert 'href="/api/v1/sources/abc/epub/chapter?i=1"' in out


def test_title_from_filename_strips_library_junk():
    from app.services.pipeline import _title_from_filename

    messy = "上头Obsidian手把手教你用AI做好知识管理 (Shiki实验室) (z-library.sk, 1lib.sk, z-lib.sk).epub"
    assert _title_from_filename(messy) == "上头Obsidian手把手教你用AI做好知识管理"
    assert _title_from_filename("21世纪的21位思想家 ([澳]麦肯齐·沃克  姜昊骞) (z-lib.sk).epub").startswith(
        "21世纪的21位思想家"
    )
    assert _title_from_filename("") == ""
    assert _title_from_filename(None) == ""
