"""Upload hardening — spec §7.9.5, Phase 5 Lane 5B. Exit criteria: **413 and 415 rejections**.

Three routes will accept files from people: contracts (20 MB, PDF or text), capacity
observations (5 MB, CSV) and complaint attachments (5 MB). An upload path is the classic
way into a system, so this module treats every byte that arrives as hostile and every
*claim* about those bytes — the ``Content-Length``, the ``Content-Type``, the filename and
its extension — as a lie until the bytes themselves say otherwise.

FOUR RULES, AND WHY EACH ONE IS WRITTEN THE WAY IT IS
-----------------------------------------------------
1. **The size limit is enforced while reading, never after.** ``len(await file.read())``
   is not a limit: by the time it returns, the 5 GB is already in this process's memory
   and the box is gone. :func:`read_limited` counts as it consumes and raises at the first
   byte past the cap, abandoning the producer — so peak memory is a function of the
   *policy*, not of what the attacker chose to send. ``Content-Length`` is checked first
   (:func:`check_declared_length`) purely to refuse a declared 5 GB without reading it; it
   is a courtesy, never the enforcement, because a chunked request need not send one and a
   liar can send any number they like.
2. **Content type comes from the bytes.** The declared header and the extension are
   attacker-controlled strings. :func:`sniff` looks at the leading bytes: ``%PDF-`` at
   offset 0 for a PDF, a strict UTF-8 decode with no NUL and no stray control characters
   for text. The PDF specification tolerates its header appearing after some leading
   junk, and readers honour that — which is precisely how a polyglot (a valid ZIP, or a
   shell script, that also contains ``%PDF-`` further in) gets treated as a PDF by one
   program and as something executable by another. This sniffer refuses that leniency:
   offset 0 or it is not a PDF.
3. **Filenames are treated as data, never as paths.** :func:`sanitise_filename` is the
   ``main.py`` ledger lesson applied to a name instead of a URL segment: the stored name
   is generated here and the user's name survives only as *metadata*. Traversal segments,
   absolute paths, drive letters, UNC prefixes, NUL bytes, alternate-data-stream colons,
   right-to-left override characters and Windows device names are all removed — and
   :func:`safe_join` then repeats the ``Path.is_relative_to`` check on the resolved path,
   because "the sanitiser already handled it" is how a sanitiser one edit away from being
   loosened becomes the only thing standing there. ``startswith`` on the string form is
   the bug fixed at ``main.py:1509``; it is not repeated here.
4. **Accepted files are stored outside any served directory, opaquely named, and never
   interpreted.** :func:`upload_root` refuses to return a directory inside ``frontend/dist``
   or the SPA's assets, :func:`store` writes ``<uuid>.bin`` (the original extension is not
   carried to disk, so nothing downstream can be tricked into executing or rendering it),
   and nothing in this module opens, unzips, renders or executes a stored file. A ZIP bomb
   is rejected at :func:`sniff` on its magic bytes — it is never expanded, which is why its
   compression ratio is irrelevant here.

WHAT THIS MODULE IS NOT
-----------------------
It is not a router and it registers nothing. It has no FastAPI import, so it can be unit
tested without an app, and the routes that will call it are owned by other lanes. With
``UPLOADS_ENABLED`` unset (the default) :func:`uploads_enabled` is False and those routes
are expected to 404 the whole lane, exactly as ``api/routers/pir.py`` does for
``PIR_ENABLED`` — so the system with the flag off behaves precisely as it does today.

The webhook half of §7.9.5 (replay nonces, the per-IP token bucket, the 256 KB body cap)
lives with the webhook routes and their table; this module deliberately covers only the
upload half.
"""

from __future__ import annotations

import csv
import hashlib
import io
import logging
import os
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping

log = logging.getLogger(__name__)

__all__ = [
    "CHUNK_BYTES",
    "KIND_CAPACITY_CSV",
    "KIND_COMPLAINT",
    "KIND_CONTRACT",
    "MEDIA_PDF",
    "MEDIA_TEXT",
    "POLICIES",
    "AcceptedUpload",
    "MalformedUpload",
    "StoredUpload",
    "UnsupportedUpload",
    "UploadPolicy",
    "UploadRejected",
    "UploadTooLarge",
    "accept",
    "check_declared_length",
    "iter_csv_rows",
    "read_limited",
    "safe_join",
    "sanitise_filename",
    "sniff",
    "store",
    "upload_root",
    "uploads_enabled",
]

ROOT = Path(__file__).resolve().parents[3]

#: How much the caller should read per iteration. Starlette's own default is 64 KiB; the
#: number matters because it is the granularity at which :func:`read_limited` can stop, so
#: worst-case memory is ``policy.max_bytes + CHUNK_BYTES``, not ``max_bytes * anything``.
CHUNK_BYTES = 64 * 1024

#: How many bytes :func:`sniff` looks at. Enough for every magic number below plus enough
#: text to catch a NUL hiding just past a plausible-looking first line.
SNIFF_BYTES = 8192

MEDIA_PDF = "application/pdf"
MEDIA_TEXT = "text/plain"  # the sniffed answer for CSV/MD/TXT alike — the bytes cannot tell them apart

KIND_CONTRACT = "CONTRACT"
KIND_CAPACITY_CSV = "CAPACITY_CSV"
KIND_COMPLAINT = "COMPLAINT"

UPLOADS_ENABLED_ENV = "UPLOADS_ENABLED"
UPLOAD_DIR_ENV = "UPLOAD_DIR"
_TRUE = ("1", "true", "yes", "on")

#: §7.9.5: "CSV parsed with the ``csv`` module, ≤ 100,000 rows".
MAX_CSV_ROWS = 100_000

#: A stored name is generated, so the original is metadata only; this cap keeps it from
#: being a 4 KB blob in a database column or a log line.
MAX_FILENAME_CHARS = 120
FALLBACK_FILENAME = "upload"


class UploadRejected(Exception):
    """Base for every refusal. ``status_code`` is the HTTP status the route must return.

    Carries a short machine ``reason`` as well as the human message: the route logs and
    audits the reason (a stable token), and shows the message. Nothing in here echoes the
    uploaded *bytes* back to the client — an error message that quotes attacker content is
    how a rejection becomes a reflection.
    """

    status_code = 400
    reason = "rejected"

    def __init__(self, message: str, *, reason: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        if reason:
            self.reason = reason


class UploadTooLarge(UploadRejected):
    """413. The body exceeded the policy cap — raised *during* the read, not after it."""

    status_code = 413
    reason = "too_large"


class UnsupportedUpload(UploadRejected):
    """415. The bytes are not a type this route accepts, whatever the client claimed."""

    status_code = 415
    reason = "unsupported_media_type"


class MalformedUpload(UploadRejected):
    """400. Structurally broken in a way that is not about size or type (an unusable name,
    a CSV that will not parse). Separated from 415 so a client can tell "wrong kind of
    file" from "right kind, broken"."""

    status_code = 400
    reason = "malformed"


@dataclass(frozen=True)
class UploadPolicy:
    """One route's rules. Caps are §7.9.5's: 20 MB contracts, 5 MB capacity, 5 MB complaints."""

    kind: str
    max_bytes: int
    #: Sniffed media types this route accepts. Membership is decided by :func:`sniff`'s
    #: answer, never by the declared header.
    allowed: frozenset[str]
    #: Extensions allowed on the *declared* name. Advisory: a wrong extension is refused
    #: early with a clear message, but a right extension buys nothing — the bytes still decide.
    extensions: frozenset[str]
    #: Roles §7.9.5 restricts the route to. Enforced by the route's ``require_role``; recorded
    #: here so the policy and the permission live in one place and cannot drift apart.
    roles: tuple[str, ...] = ()


POLICIES: dict[str, UploadPolicy] = {
    KIND_CONTRACT: UploadPolicy(
        kind=KIND_CONTRACT,
        max_bytes=20 * 1024 * 1024,
        allowed=frozenset({MEDIA_PDF, MEDIA_TEXT}),
        extensions=frozenset({".pdf", ".md", ".txt"}),
        roles=("admin", "legal"),
    ),
    KIND_CAPACITY_CSV: UploadPolicy(
        kind=KIND_CAPACITY_CSV,
        max_bytes=5 * 1024 * 1024,
        allowed=frozenset({MEDIA_TEXT}),  # no PDF: a PRB export is text or it is not a PRB export
        extensions=frozenset({".csv", ".txt"}),
        roles=("admin", "planning"),
    ),
    KIND_COMPLAINT: UploadPolicy(
        kind=KIND_COMPLAINT,
        max_bytes=5 * 1024 * 1024,
        allowed=frozenset({MEDIA_PDF, MEDIA_TEXT}),
        extensions=frozenset({".pdf", ".txt", ".md", ".csv"}),
        roles=("admin", "legal"),
    ),
}


def uploads_enabled() -> bool:
    """``UPLOADS_ENABLED`` — default false, so the lane ships inert (rule: flags off = today's
    behaviour). The routes 404 while this is False; this module stays importable and callable
    either way, because a pure validator has no side effect to gate."""
    return (os.getenv(UPLOADS_ENABLED_ENV) or "").strip().lower() in _TRUE


def policy_for(kind: str) -> UploadPolicy:
    try:
        return POLICIES[(kind or "").strip().upper()]
    except KeyError:  # an unknown kind is a programming error, not a client error
        raise ValueError(f"unknown upload kind {kind!r}; known kinds: {sorted(POLICIES)}") from None


# --------------------------------------------------------------------------- filenames

#: Windows device names. These are devices at **every** directory level and with **any**
#: extension: ``NUL.txt``, ``C:\\data\\uploads\\CON.pdf`` and ``COM1`` all open a device
#: rather than a file, and writing to one can hang the process. The superscript forms
#: (``COM¹``/``COM²``/``COM³``) are the ones that get forgotten: Win32 maps them to COM1/2/3
#: as well, so a name normalised only over ASCII sails straight through.
_WINDOWS_DEVICES: frozenset[str] = frozenset(
    {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}
    | {f"COM{n}" for n in "123456789"}
    | {f"LPT{n}" for n in "123456789"}
    | {f"COM{c}" for c in "\u00b9\u00b2\u00b3"}
    | {f"LPT{c}" for c in "\u00b9\u00b2\u00b3"}
)

#: Reserved on NTFS, plus the separators of both platforms. ``:`` is not cosmetic — it opens
#: an NTFS alternate data stream (``report.pdf:evil.exe``), which is a file the directory
#: listing does not show.
_UNSAFE_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f\x7f]')
#: Bidirectional overrides: ``invoice\u202Egnp.exe`` renders to a human as ``invoiceexe.png``.
_BIDI_OVERRIDES = re.compile(r"[\u202a-\u202e\u2066-\u2069\u200e\u200f]")
_DOTS_ONLY = re.compile(r"^\.+$")


def sanitise_filename(raw: str | None) -> str:
    """Reduce a client-supplied filename to something safe to *record*. Never a path.

    This is not how the file is stored — :func:`store` generates an opaque name — so the
    result is only ever shown to a human and kept as metadata. It is still hardened,
    because metadata reaches log lines, spreadsheet cells, ``Content-Disposition`` headers
    and, eventually, somebody's shell.

    Removed, in order: NUL and control characters; bidi overrides; every path separator of
    both platforms (by taking the last segment of each); a drive letter or UNC prefix; the
    NTFS-reserved characters; leading dashes (a name beginning ``-`` is an option the first
    time it reaches a command line); leading and trailing dots and spaces (Win32 strips
    these itself, which is what makes ``"CON. "`` the CON device); and a Windows device
    name, which is suffixed rather than blanked so the human can still see what was sent.
    """
    name = unicodedata.normalize("NFC", str(raw or ""))
    name = _BIDI_OVERRIDES.sub("", name)
    name = name.split("\x00", 1)[0]  # a NUL truncates in C; truncate here too, deliberately
    # Both separators, both platforms: a POSIX server still receives Windows names, and a
    # Windows server still receives "../". Taking the last segment of each defeats
    # "..\\..\\etc\\passwd" and "../../etc/passwd" identically, and also strips "C:" and the
    # "\\\\host\\share" UNC prefix without needing to recognise either.
    name = name.replace("\\", "/").rsplit("/", 1)[-1]
    name = _UNSAFE_CHARS.sub("_", name)
    name = name.lstrip("-").strip(" .")
    if not name or _DOTS_ONLY.match(name):
        return FALLBACK_FILENAME
    stem = name.split(".", 1)[0].strip().upper()
    if stem in _WINDOWS_DEVICES:
        # Not blanked: "CON.pdf" becoming "upload" hides from the reviewer what was actually
        # attempted. "CON.pdf" -> "CON_device.pdf" keeps the evidence and kills the device.
        name = name.replace(name.split(".", 1)[0], name.split(".", 1)[0] + "_device", 1)
    if len(name) > MAX_FILENAME_CHARS:
        stem_part, dot, ext = name.rpartition(".")
        if dot and 0 < len(ext) <= 10:
            keep = max(1, MAX_FILENAME_CHARS - len(ext) - 1)
            name = stem_part[:keep] + "." + ext
        else:
            name = name[:MAX_FILENAME_CHARS]
    return name or FALLBACK_FILENAME


def safe_join(root: Path, name: str) -> Path:
    """``root / name``, resolved, with the ``is_relative_to`` guard — or ``MalformedUpload``.

    The second gate, exactly as ``main.py`` keeps one behind its ``shift_id`` pattern: "the
    pattern already makes this unreachable — it is here because a pattern is one edit away
    from being loosened, and because the resolved path, not the string, is what a traversal
    actually escapes with". ``str(candidate).startswith(str(root))`` is the wrong comparison
    (``data/uploads-backup`` passes it); ``is_relative_to`` compares path components.
    """
    base = Path(root).resolve()
    candidate = (base / sanitise_filename(name)).resolve()
    if not candidate.is_relative_to(base):
        raise MalformedUpload("filename escapes the upload directory", reason="path_traversal")
    return candidate


# --------------------------------------------------------------------------- storage root


def upload_root() -> Path:
    """Where accepted files land: ``UPLOAD_DIR``, else ``data/uploads``. Never a served path.

    The check is the point. ``main.py`` mounts ``frontend/dist`` and ``frontend/dist/assets``
    as static directories, so an upload directory placed inside either one turns every
    accepted file into a URL — stored XSS for an HTML file, arbitrary download for the rest.
    A misconfigured ``UPLOAD_DIR`` therefore falls back to the default with a warning rather
    than being honoured; refusing to start is not this module's call to make, but silently
    publishing uploads is not either.
    """
    default = ROOT / "data" / "uploads"
    raw = (os.getenv(UPLOAD_DIR_ENV) or "").strip()
    chosen = Path(raw).expanduser().resolve() if raw else default.resolve()
    served = [(ROOT / "frontend" / "dist").resolve(), (ROOT / "frontend" / "dist" / "assets").resolve()]
    if any(chosen == path or chosen.is_relative_to(path) for path in served):
        log.warning("%s=%s is inside a statically served directory; using %s instead", UPLOAD_DIR_ENV, chosen, default)
        chosen = default.resolve()
    return chosen


# --------------------------------------------------------------------------- size


def check_declared_length(policy: UploadPolicy, content_length: int | str | None) -> None:
    """Refuse an oversized ``Content-Length`` before a single byte is read (413).

    A hint, not the limit. A chunked request carries no ``Content-Length`` at all, and one
    that does can simply lie — a 5 GB body may declare 10 bytes. This exists so the honest
    5 GB upload is rejected in one round trip instead of after five gigabytes crossed the
    wire; :func:`read_limited` is what actually holds the line.
    """
    if content_length in (None, ""):
        return
    try:
        declared = int(content_length)
    except (TypeError, ValueError):
        raise MalformedUpload("Content-Length is not a number", reason="bad_content_length") from None
    if declared < 0:
        raise MalformedUpload("Content-Length is negative", reason="bad_content_length")
    if declared > policy.max_bytes:
        raise UploadTooLarge(
            f"upload declares {declared} bytes; the limit for {policy.kind} is {policy.max_bytes} bytes"
        )


def read_limited(chunks: Iterable[bytes], limit: int) -> Iterator[bytes]:
    """Yield chunks while the running total stays within ``limit``; raise 413 the moment it does not.

    The generator stops pulling from ``chunks`` as soon as the cap is crossed, so a producer
    that would have yielded 5 GB is abandoned after ``limit`` bytes plus one chunk. That
    "abandoned" is the whole mechanism, and ``tests/unit/test_uploads.py`` asserts it by
    counting how many chunks a deliberately enormous generator was asked for.

    A single chunk larger than the limit is also caught here: the check is on the running
    total *after* adding the chunk, so one 5 GB ``read()`` handed in whole is rejected — but
    by then the caller has already allocated it, which is why the docstring of this module
    tells callers to read in ``CHUNK_BYTES`` pieces rather than in one call.
    """
    total = 0
    for chunk in chunks:
        if not chunk:
            continue
        total += len(chunk)
        if total > limit:
            raise UploadTooLarge(f"upload exceeds the {limit}-byte limit for this route")
        yield chunk


# --------------------------------------------------------------------------- sniffing

#: Signatures that are refused outright, whatever the route. Each is named so the audit row
#: says *what* was sent, which is the difference between "somebody fat-fingered a file" and
#: "somebody tried to upload a PE binary to the contracts endpoint".
_REFUSED_MAGIC: tuple[tuple[bytes, str], ...] = (
    # A ZIP header is also a .docx, .xlsx, .jar, .apk and every ZIP bomb ever written. The
    # bomb is rejected *here*, on four bytes, and is never opened — which is why its
    # compression ratio never becomes this process's problem.
    (b"PK\x03\x04", "zip archive"),
    (b"PK\x05\x06", "empty zip archive"),
    (b"PK\x07\x08", "spanned zip archive"),
    (b"\x1f\x8b", "gzip stream"),
    (b"BZh", "bzip2 stream"),
    (b"\xfd7zXZ\x00", "xz stream"),
    (b"7z\xbc\xaf\x27\x1c", "7-zip archive"),
    (b"Rar!\x1a\x07", "rar archive"),
    (b"MZ", "windows executable"),
    (b"\x7fELF", "elf executable"),
    (b"\xca\xfe\xba\xbe", "java class or mach-o fat binary"),
    (b"\xd0\xcf\x11\xe0", "ole compound file (legacy doc/xls)"),
    (b"{\\rtf", "rtf document"),  # RTF embeds OLE objects; not on any accept list here
    (b"%!PS", "postscript"),  # a programming language with file I/O, not a document format
    (b"#!", "script with a shebang"),
    (b"\x89PNG", "png image"),
    (b"\xff\xd8\xff", "jpeg image"),
    (b"GIF8", "gif image"),
)

#: Text that must not be accepted as text. A ``.csv`` whose first bytes are ``<script>`` is
#: valid UTF-8 and would otherwise pass the text sniffer, and it only has to be served once
#: — or opened once from the reviewer's downloads folder — to run.
_REFUSED_TEXT_PREFIXES: tuple[tuple[str, str], ...] = (
    ("<?php", "php source"),
    ("<script", "html/script fragment"),
    ("<html", "html document"),
    ("<!doctype", "html document"),
    ("<svg", "svg (scriptable) image"),
    ("<?xml", "xml document"),
)

#: Control characters that have no business in a text upload. TAB, LF, CR and FF are the
#: legitimate ones; everything else in C0 plus DEL means these bytes are not text, and NUL
#: in particular is both the classic binary marker and what makes Python's ``csv`` module
#: raise. Checked on the sniff window, and again over the whole body in :func:`_decode_text`.
_TEXT_CONTROL = re.compile(r"[\x00-\x08\x0b\x0e-\x1f\x7f]")


@dataclass(frozen=True)
class SniffResult:
    media_type: str
    detail: str


def sniff(head: bytes) -> SniffResult:
    """Decide what these bytes actually are. Raises :class:`UnsupportedUpload` (415) if unknown.

    Order matters and is deliberate: the refused signatures are checked **before** the PDF
    and text tests, so a file that is both — the polyglot — is refused rather than accepted
    on its second identity. And the PDF test is anchored at offset 0. Readers in the wild
    accept ``%PDF-`` a few hundred bytes in, and that tolerance is exactly how a ZIP or a
    script gets to also be "a PDF"; a file that needs that leniency is not one this NOC has
    any reason to accept.
    """
    if not head:
        raise UnsupportedUpload("the upload is empty", reason="empty")
    for magic, label in _REFUSED_MAGIC:
        if head.startswith(magic):
            raise UnsupportedUpload(f"{label} is not an accepted upload type", reason="refused_signature")
    if head.startswith(b"%PDF-"):
        return SniffResult(MEDIA_PDF, "pdf header at offset 0")
    # Anchored above; if %PDF- appears anywhere else the file is a polyglot, and saying so
    # in the rejection is worth more to the reviewer than a generic "unsupported".
    if b"%PDF-" in head[:SNIFF_BYTES]:
        raise UnsupportedUpload(
            "the PDF header is not at the start of the file; polyglot files are not accepted",
            reason="pdf_header_not_at_offset_zero",
        )
    text = _try_decode(head)
    if text is None:
        raise UnsupportedUpload("the upload is neither a PDF nor UTF-8 text", reason="unrecognised_bytes")
    if _TEXT_CONTROL.search(text):
        raise UnsupportedUpload("the upload contains control bytes and is not text", reason="control_bytes")
    stripped = text.lstrip().lower()
    for prefix, label in _REFUSED_TEXT_PREFIXES:
        if stripped.startswith(prefix):
            raise UnsupportedUpload(f"{label} is not an accepted upload type", reason="refused_text")
    return SniffResult(MEDIA_TEXT, "utf-8 text")


def _try_decode(data: bytes) -> str | None:
    """Strict UTF-8, tolerating a BOM and a truncated final character.

    The truncation allowance is for the sniff window only: ``SNIFF_BYTES`` can land in the
    middle of a multi-byte character, and failing a legitimate Swahili site name because the
    window cut it in half would be a bug, not security. The whole body is decoded strictly
    later, in :func:`_decode_text`, where no such allowance applies.
    """
    body = data[3:] if data.startswith(b"\xef\xbb\xbf") else data
    for trim in range(0, 4):
        candidate = body[: len(body) - trim] if trim else body
        try:
            return candidate.decode("utf-8")
        except UnicodeDecodeError:
            continue
    return None


def _decode_text(data: bytes) -> str:
    """The whole body as text, strictly. 415 rather than ``errors="replace"``.

    Replacing undecodable bytes would silently change the contract or the PRB figures being
    stored — a file that is not valid UTF-8 is not a file this system should be pretending
    to have read correctly.
    """
    body = data[3:] if data.startswith(b"\xef\xbb\xbf") else data
    try:
        return body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise UnsupportedUpload("the upload is not valid UTF-8 text", reason="not_utf8") from exc


# --------------------------------------------------------------------------- acceptance


@dataclass(frozen=True)
class AcceptedUpload:
    """A body that passed every check. ``data`` is bounded by ``policy.max_bytes``, always."""

    kind: str
    data: bytes
    media_type: str
    size: int
    sha256: str
    filename: str  # sanitised; metadata only — never used to build a path
    declared_content_type: str
    detail: str

    @property
    def text(self) -> str:
        """The body as text. Raises 415 for a PDF — callers that want both check the type."""
        if self.media_type != MEDIA_TEXT:
            raise UnsupportedUpload("this upload is not text", reason="not_text")
        return _decode_text(self.data)


def accept(
    kind: str,
    chunks: Iterable[bytes],
    *,
    filename: str | None = None,
    declared_content_type: str | None = None,
    content_length: int | str | None = None,
) -> AcceptedUpload:
    """Validate an upload end to end and return it, or raise an :class:`UploadRejected`.

    The one entry point a route needs. The body is consumed through :func:`read_limited`, so
    **this function never holds more than the policy cap** regardless of what the client
    sends; that buffer is the reason a rejected file is never written to disk (§7.9.5) —
    :func:`store` is a separate call that only ever sees an :class:`AcceptedUpload`.

    ``declared_content_type`` and the extension are used to *refuse early with a clear
    message* and to detect a client whose claim contradicts its bytes. Neither can make an
    upload acceptable: membership of ``policy.allowed`` is decided by :func:`sniff`.
    """
    policy = policy_for(kind)
    check_declared_length(policy, content_length)

    safe_name = sanitise_filename(filename)
    declared = (declared_content_type or "").split(";", 1)[0].strip().lower()
    suffix = Path(safe_name).suffix.lower()
    if suffix and policy.extensions and suffix not in policy.extensions:
        raise UnsupportedUpload(
            f"{suffix} files are not accepted here (accepted: {', '.join(sorted(policy.extensions))})",
            reason="extension_not_allowed",
        )

    buffer = io.BytesIO()
    digest = hashlib.sha256()
    for chunk in read_limited(chunks, policy.max_bytes):
        buffer.write(chunk)
        digest.update(chunk)
    data = buffer.getvalue()
    if not data:
        raise UnsupportedUpload("the upload is empty", reason="empty")

    result = sniff(data[:SNIFF_BYTES])
    if result.media_type not in policy.allowed:
        raise UnsupportedUpload(
            f"{result.media_type} is not accepted by the {policy.kind} route "
            f"(accepted: {', '.join(sorted(policy.allowed))})",
            reason="media_type_not_allowed",
        )
    # The declared type disagreeing with the bytes is not a formatting quirk; it is the
    # signature of a client dressing one thing up as another. Refused rather than corrected.
    if declared and declared not in _compatible_declared(result.media_type):
        raise UnsupportedUpload(
            f"declared Content-Type {declared!r} does not match the file's actual content",
            reason="declared_type_mismatch",
        )
    if result.media_type == MEDIA_TEXT:
        text = _decode_text(data)  # strict over the whole body, not just the sniff window
        if _TEXT_CONTROL.search(text):
            raise UnsupportedUpload("the upload contains control bytes and is not text", reason="control_bytes")
    return AcceptedUpload(
        kind=policy.kind,
        data=data,
        media_type=result.media_type,
        size=len(data),
        sha256=digest.hexdigest(),
        filename=safe_name,
        declared_content_type=declared,
        detail=result.detail,
    )


def _compatible_declared(media_type: str) -> frozenset[str]:
    """Declared types that may legitimately accompany a sniffed type.

    Browsers and curl send a range of spellings for the same thing (``text/csv``,
    ``application/csv``, ``text/markdown``, and ``application/octet-stream`` whenever the OS
    has no opinion), so the comparison is against a set rather than one string. It is still
    a *mismatch* check, not a whitelist: nothing in here can make an unacceptable file
    acceptable.
    """
    if media_type == MEDIA_PDF:
        return frozenset({"application/pdf", "application/x-pdf", "application/octet-stream", "binary/octet-stream"})
    return frozenset(
        {
            "text/plain",
            "text/csv",
            "text/markdown",
            "text/x-markdown",
            "application/csv",
            "application/octet-stream",
            "binary/octet-stream",
        }
    )


# --------------------------------------------------------------------------- CSV


def iter_csv_rows(text: str, *, max_rows: int = MAX_CSV_ROWS) -> Iterator[list[str]]:
    """Parse with the ``csv`` module, capped at ``max_rows`` (§7.9.5) — 413 beyond it.

    ``csv`` and not ``str.split(",")``: quoted fields containing commas and newlines are
    normal in a PRB export, and a hand-rolled split silently shifts every column after the
    first quoted comma. The row cap is a second 413 because a 5 MB file of ``\\n`` is only
    5 MB on the wire but is five million rows in the database.
    """
    reader = csv.reader(io.StringIO(text, newline=""))
    count = 0
    try:
        for row in reader:
            count += 1
            if count > max_rows:
                raise UploadTooLarge(f"CSV exceeds {max_rows} rows", reason="too_many_rows")
            yield row
    except csv.Error as exc:  # includes the NUL-byte case, which sniff should already have caught
        raise MalformedUpload(f"CSV could not be parsed: {type(exc).__name__}", reason="bad_csv") from exc


# --------------------------------------------------------------------------- storage


@dataclass(frozen=True)
class StoredUpload:
    """What ended up on disk. ``stored_name`` is opaque; ``filename`` is the human's version."""

    path: Path
    stored_name: str
    filename: str
    media_type: str
    size: int
    sha256: str
    meta: Mapping[str, Any] = field(default_factory=dict)


def store(accepted: AcceptedUpload, *, root: Path | None = None, stored_name: str | None = None) -> StoredUpload:
    """Write an accepted upload under :func:`upload_root` with a generated, opaque name.

    Only an :class:`AcceptedUpload` can reach here, which is how §7.9.5's "rejected files
    never written to disk" is structural rather than a rule somebody has to remember.

    The stored name is ``<uuid>.bin`` — the client's extension is deliberately **not**
    carried across. An ``.html`` in a directory that is ever mis-served is stored XSS; a
    ``.pdf`` invites a downstream tool to render it; ``.bin`` invites nothing. The original
    name travels as metadata for the reviewer, and the ``.bin`` on disk is looked up through
    the database row, never by the name a user chose.
    """
    base = Path(root).resolve() if root is not None else upload_root()
    base.mkdir(parents=True, exist_ok=True)
    name = stored_name or f"{_opaque_id()}.bin"
    if not re.fullmatch(r"[A-Za-z0-9_-]{8,64}\.bin", name):
        raise MalformedUpload("stored_name must be an opaque <id>.bin", reason="bad_stored_name")
    path = safe_join(base, name)  # belt and braces: the name is generated, and still checked
    # "xb" — exclusive create. An existing file is never overwritten, so a collision (or a
    # planted file) is an error rather than a silent replacement of somebody's contract.
    with open(path, "xb") as handle:
        handle.write(accepted.data)
    try:
        os.chmod(path, 0o600)  # no-op on Windows; on POSIX it keeps the file off other accounts
    except OSError:  # pragma: no cover - filesystem without permission support
        pass
    return StoredUpload(
        path=path,
        stored_name=name,
        filename=accepted.filename,
        media_type=accepted.media_type,
        size=accepted.size,
        sha256=accepted.sha256,
        meta={"kind": accepted.kind, "declared_content_type": accepted.declared_content_type, "detail": accepted.detail},
    )


def _opaque_id() -> str:
    """``db.models.new_id`` when it is importable, else ``uuid4``.

    Late and guarded for the reason ``services/evidence.py`` gives: this module must be
    importable and unit-testable without the ORM, and an id generator is not worth a hard
    dependency on it.
    """
    try:
        from noc_agents.db.models import new_id  # noqa: PLC0415 - deliberate late import

        return str(new_id()).replace("-", "")[:32] or _uuid()
    except Exception:  # noqa: BLE001
        return _uuid()


def _uuid() -> str:
    import uuid

    return uuid.uuid4().hex


# --------------------------------------------------------------------------- audit


def rejection_payload(exc: UploadRejected, *, kind: str, filename: str | None, declared: str | None) -> dict[str, Any]:
    """The fields a route puts on its ``AuditRow(action="upload.rejected")``.

    §7.9.5 requires an audit row for every webhook rejection; an upload rejection deserves
    the same, and for the same reason — a refusal nobody can count is indistinguishable from
    an endpoint nobody is attacking. The payload carries the *sanitised* filename and the
    machine reason, and **never the uploaded bytes**: the audit table is read by people, and
    quoting attacker content into it moves the payload rather than stopping it.
    """
    return {
        "kind": (kind or "").strip().upper(),
        "reason": exc.reason,
        "status": exc.status_code,
        "filename": sanitise_filename(filename),
        "declared_content_type": (declared or "").split(";", 1)[0].strip().lower(),
        "message": exc.message,
    }
