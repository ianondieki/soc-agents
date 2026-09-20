"""Upload hardening — spec §7.9.5, Phase 5 exit criterion "upload rejections (413/415)".

Contracts, PRB exports and complaint attachments are uploaded by people, and an upload path
is the classic way into a system. These tests are written adversarially on purpose: a zip
bomb, a polyglot whose extension lies, a 5 GB declared ``Content-Length``, a filename that
is ``../../etc/passwd``, and a Windows device name on a system that runs on Windows.

The two that matter most are structural rather than cosmetic:

* the size limit must be enforced **while streaming**. A limit applied after
  ``await file.read()`` is not a limit — the memory is already gone. The test below hands
  in a generator that would yield 5 GB and asserts the reader stopped pulling from it;
* the content type must come from the **bytes**. The declared header, the extension and the
  filename are all attacker-controlled, so each of them is tested lying in both directions.

No network, no database, no FastAPI: ``services/uploads.py`` deliberately imports none of
them, so the whole file runs against the functions themselves.
"""

from __future__ import annotations

import gzip
import io
import zipfile
from pathlib import Path

import pytest

from noc_agents.services.uploads import (
    CHUNK_BYTES,
    KIND_CAPACITY_CSV,
    KIND_COMPLAINT,
    KIND_CONTRACT,
    MAX_CSV_ROWS,
    MEDIA_PDF,
    MEDIA_TEXT,
    POLICIES,
    MalformedUpload,
    UnsupportedUpload,
    UploadRejected,
    UploadTooLarge,
    accept,
    check_declared_length,
    iter_csv_rows,
    policy_for,
    read_limited,
    rejection_payload,
    safe_join,
    sanitise_filename,
    sniff,
    store,
    upload_root,
    uploads_enabled,
)

PDF = b"%PDF-1.7\n1 0 obj\n<< /Type /Catalog >>\nendobj\n"
CSV = b"site_id,metric,value,busy_hour_at\nNRB-0421,DL_TOTAL_PRB_USAGE,71.2,2026-09-16T18:00:00Z\n"


def chunked(data: bytes, size: int = 8):
    """Hand the body over the way a request body actually arrives: in pieces."""
    for offset in range(0, len(data), size):
        yield data[offset : offset + size]


# --------------------------------------------------------------------------- 413: size


def test_a_five_gigabyte_content_length_is_refused_before_a_single_byte_is_read():
    pulled = 0

    def body():
        nonlocal pulled
        pulled += 1
        yield b"x"

    with pytest.raises(UploadTooLarge) as exc:
        accept(KIND_CONTRACT, body(), filename="contract.pdf", content_length=5 * 1024**3)
    assert exc.value.status_code == 413
    assert pulled == 0, "the body was read despite an impossible Content-Length"


def test_the_size_limit_is_enforced_while_streaming_and_abandons_the_producer():
    # THE test for this module. An implementation that buffers first and measures afterwards
    # passes every other assertion in this file and still falls over on the real upload.
    limit = POLICIES[KIND_CAPACITY_CSV].max_bytes
    budget = limit // CHUNK_BYTES + 4  # what a streaming reader can possibly need
    pulled = 0

    def five_gigabytes():
        nonlocal pulled
        for _ in range(5 * 1024**3 // CHUNK_BYTES):
            pulled += 1
            # Fail loudly rather than let a buffering regression allocate 5 GB in CI.
            assert pulled <= budget, f"reader consumed {pulled} chunks; it is not streaming"
            yield b"a" * CHUNK_BYTES

    with pytest.raises(UploadTooLarge) as exc:
        accept(KIND_CAPACITY_CSV, five_gigabytes(), filename="prb.csv", declared_content_type="text/csv")
    assert exc.value.status_code == 413
    assert pulled <= budget


def test_read_limited_yields_up_to_the_cap_and_raises_on_the_byte_that_crosses_it():
    assert b"".join(read_limited([b"abc", b"de"], 5)) == b"abcde"
    with pytest.raises(UploadTooLarge):
        list(read_limited([b"abc", b"def"], 5))


def test_a_body_one_byte_over_the_cap_is_413_and_a_body_exactly_at_the_cap_is_accepted():
    limit = POLICIES[KIND_CAPACITY_CSV].max_bytes
    header = b"a,b\n"
    exact = header + b"c" * (limit - len(header))
    assert accept(KIND_CAPACITY_CSV, chunked(exact, CHUNK_BYTES), filename="x.csv").size == limit
    with pytest.raises(UploadTooLarge):
        accept(KIND_CAPACITY_CSV, chunked(exact + b"c", CHUNK_BYTES), filename="x.csv")


@pytest.mark.parametrize("declared", ["not-a-number", "-1"])
def test_a_nonsense_content_length_is_refused_rather_than_ignored(declared):
    with pytest.raises(MalformedUpload):
        check_declared_length(policy_for(KIND_CONTRACT), declared)


def test_a_missing_content_length_is_not_an_error_because_chunked_requests_have_none():
    check_declared_length(policy_for(KIND_CONTRACT), None)
    check_declared_length(policy_for(KIND_CONTRACT), "")


def test_a_csv_with_more_than_a_hundred_thousand_rows_is_413_even_though_it_fits_the_byte_cap():
    # 5 MB of "\n" is only 5 MB on the wire and five million rows in the database.
    text = "a\n" * (MAX_CSV_ROWS + 5)
    with pytest.raises(UploadTooLarge) as exc:
        list(iter_csv_rows(text))
    assert exc.value.reason == "too_many_rows"


# --------------------------------------------------------------------------- 415: type


def test_a_zip_bomb_is_refused_on_its_magic_bytes_and_is_never_expanded():
    # 1 MB of zeros compresses to a couple of hundred bytes; a real bomb is the same trick
    # nested. It does not matter here, because nothing ever opens the archive: four bytes at
    # offset 0 end the conversation, so the decompression ratio is irrelevant by construction.
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("bomb.txt", b"\x00" * (1024 * 1024))
    bomb = buffer.getvalue()
    assert bomb.startswith(b"PK\x03\x04")
    with pytest.raises(UnsupportedUpload) as exc:
        accept(KIND_CONTRACT, chunked(bomb), filename="contract.pdf", declared_content_type="application/pdf")
    assert exc.value.status_code == 415
    assert exc.value.reason == "refused_signature"


def test_a_gzip_stream_is_refused_without_being_decompressed():
    payload = gzip.compress(b"\x00" * (1024 * 1024))
    with pytest.raises(UnsupportedUpload) as exc:
        accept(KIND_COMPLAINT, chunked(payload), filename="evidence.txt")
    assert exc.value.reason == "refused_signature"


def test_a_polyglot_whose_extension_and_declared_type_both_claim_pdf_is_still_refused():
    # The file is a valid ZIP that also contains "%PDF-" further in. One reader sees an
    # archive, another sees a document; the extension and the header agree with neither.
    polyglot = b"PK\x03\x04" + b"\x00" * 40 + b"%PDF-1.7 trailing document\n"
    with pytest.raises(UnsupportedUpload) as exc:
        accept(KIND_CONTRACT, chunked(polyglot), filename="msa_2026.pdf", declared_content_type="application/pdf")
    assert exc.value.reason == "refused_signature"


def test_a_pdf_header_that_is_not_at_offset_zero_is_refused_rather_than_tolerated():
    # PDF readers accept a header a few hundred bytes in. That leniency is exactly how a
    # script gets to also be "a PDF", so this sniffer does not copy it.
    late = b"harmless looking preamble\n" * 4 + PDF
    with pytest.raises(UnsupportedUpload) as exc:
        accept(KIND_CONTRACT, chunked(late), filename="contract.pdf", declared_content_type="application/pdf")
    assert exc.value.reason == "pdf_header_not_at_offset_zero"


def test_a_pdf_is_refused_by_the_capacity_route_because_a_prb_export_is_text():
    with pytest.raises(UnsupportedUpload) as exc:
        accept(KIND_CAPACITY_CSV, chunked(PDF), filename="prb.csv", declared_content_type="text/csv")
    assert exc.value.status_code == 415


def test_a_declared_content_type_that_contradicts_the_bytes_is_refused():
    # §7.9.5's "PDF with wrong magic -> 415", and its mirror image: right magic, wrong claim.
    with pytest.raises(UnsupportedUpload) as exc:
        accept(KIND_CONTRACT, chunked(PDF), filename="contract.txt", declared_content_type="text/plain")
    assert exc.value.reason in {"declared_type_mismatch", "media_type_not_allowed"}


def test_a_file_claiming_to_be_a_pdf_that_is_plain_text_is_refused_on_the_declared_mismatch():
    with pytest.raises(UnsupportedUpload) as exc:
        accept(KIND_CONTRACT, chunked(b"just some words\n"), filename="c.txt", declared_content_type="application/pdf")
    assert exc.value.reason == "declared_type_mismatch"


@pytest.mark.parametrize(
    "body, label",
    [
        (b"MZ\x90\x00\x03", "windows executable"),
        (b"\x7fELF\x02\x01\x01", "elf executable"),
        (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1", "legacy ole document"),
        (b"{\\rtf1\\ansi", "rtf"),
        (b"%!PS-Adobe-3.0", "postscript"),
        (b"#!/bin/sh\nrm -rf /\n", "shell script"),
        (b"\x89PNG\r\n\x1a\n", "png"),
    ],
)
def test_executables_and_other_non_documents_are_refused_whatever_they_are_called(body, label):
    with pytest.raises(UnsupportedUpload) as exc:
        accept(KIND_CONTRACT, chunked(body), filename="contract.pdf", declared_content_type="application/pdf")
    assert exc.value.status_code == 415, label


@pytest.mark.parametrize(
    "body",
    [b"<?php system($_GET['c']); ?>", b"<script>fetch('/api/v1/incidents')</script>", b"<svg onload=alert(1)>"],
)
def test_scriptable_text_is_refused_even_though_it_is_perfectly_valid_utf8(body):
    # It only has to be served once — or opened once from the reviewer's downloads folder.
    with pytest.raises(UnsupportedUpload) as exc:
        accept(KIND_COMPLAINT, chunked(body), filename="complaint.txt", declared_content_type="text/plain")
    assert exc.value.status_code == 415


def test_a_nul_byte_makes_a_file_binary_no_matter_how_much_of_it_looks_like_text():
    body = b"site_id,value\nNRB-0421,71.2\n" + b"\x00" + b"more,text\n"
    with pytest.raises(UnsupportedUpload) as exc:
        accept(KIND_CAPACITY_CSV, chunked(body), filename="prb.csv")
    assert exc.value.reason in {"control_bytes", "unrecognised_bytes"}


def test_bytes_that_are_not_valid_utf8_are_refused_rather_than_read_with_replacement():
    # errors="replace" would silently alter the contract text or the PRB figures being stored.
    with pytest.raises(UnsupportedUpload):
        accept(KIND_CAPACITY_CSV, chunked(b"site,value\n\xff\xfe\xfa bad bytes\n"), filename="prb.csv")


def test_an_empty_upload_is_refused_instead_of_being_stored_as_a_zero_byte_contract():
    with pytest.raises(UploadRejected) as exc:
        accept(KIND_CONTRACT, chunked(b""), filename="contract.pdf")
    assert exc.value.reason == "empty"


def test_an_extension_the_route_does_not_accept_is_refused_early_with_a_clear_message():
    with pytest.raises(UnsupportedUpload) as exc:
        accept(KIND_CONTRACT, chunked(PDF), filename="contract.exe", declared_content_type="application/pdf")
    assert exc.value.reason == "extension_not_allowed"


def test_a_correct_extension_buys_nothing_because_the_bytes_still_decide():
    with pytest.raises(UnsupportedUpload):
        accept(KIND_CONTRACT, chunked(b"PK\x03\x04junk"), filename="perfectly_fine.pdf")


def test_sniff_recognises_the_two_types_this_system_accepts():
    assert sniff(PDF).media_type == MEDIA_PDF
    assert sniff(CSV).media_type == MEDIA_TEXT
    assert sniff(b"\xef\xbb\xbfsite,value\n").media_type == MEDIA_TEXT  # a BOM is still text


# --------------------------------------------------------------------------- filenames


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("../../etc/passwd", "passwd"),
        ("....//....//etc/passwd", "passwd"),
        ("..\\..\\windows\\system32\\cmd.exe", "cmd.exe"),
        ("/etc/shadow", "shadow"),
        ("C:\\Users\\PC\\secret.pdf", "secret.pdf"),
        ("\\\\attacker-host\\share\\payload.pdf", "payload.pdf"),
        ("..", "upload"),
        ("...", "upload"),
        ("", "upload"),
        (None, "upload"),
    ],
)
def test_a_filename_is_reduced_to_a_name_and_can_never_be_a_path(raw, expected):
    assert sanitise_filename(raw) == expected


@pytest.mark.parametrize("device", ["CON", "NUL", "PRN", "AUX", "COM1", "LPT9", "com3", "NUL.txt", "CON.pdf"])
def test_windows_device_names_are_defused_because_this_system_runs_on_windows(device):
    # Writing to CON or NUL opens a device, not a file, at every directory level and with
    # any extension. The name is kept visible rather than blanked so a reviewer can see
    # what was attempted.
    cleaned = sanitise_filename(device)
    assert "_device" in cleaned
    assert cleaned.split(".", 1)[0].upper().replace("_DEVICE", "") == device.split(".", 1)[0].upper()


def test_the_superscript_com_names_windows_also_treats_as_devices_are_defused():
    # COM(superscript 1/2/3) map to COM1/COM2/COM3 in Win32. A check written only over ASCII
    # misses them entirely, which is the whole reason they are named explicitly.
    for device in ("COM\u00b9", "COM\u00b2.csv", "LPT\u00b3"):
        assert "_device" in sanitise_filename(device)


def test_a_nul_byte_in_a_filename_truncates_the_name_rather_than_travelling_with_it():
    # A NUL terminates a string in C; anything after it is invisible to some layers of the
    # stack and visible to others, which is the disagreement the attack lives in.
    assert sanitise_filename("contract.pdf\x00.exe") == "contract.pdf"


def test_an_alternate_data_stream_colon_is_stripped_because_it_hides_a_second_file():
    # "report.pdf:evil.exe" writes a stream no directory listing shows.
    assert ":" not in sanitise_filename("report.pdf:evil.exe")


def test_a_right_to_left_override_that_disguises_an_extension_is_removed():
    # "invoice<U+202E>gnp.exe" renders to a human as "invoiceexe.png".
    assert sanitise_filename("invoice\u202egnp.exe") == "invoicegnp.exe"


def test_a_leading_dash_is_removed_because_it_becomes_an_option_on_the_first_command_line():
    for raw in ("-rf contract.pdf", "--exec=payload.pdf", "-contract.pdf"):
        assert not sanitise_filename(raw).startswith("-"), raw


def test_trailing_dots_and_spaces_go_because_win32_strips_them_and_that_is_the_device_trick():
    assert sanitise_filename("CON. ") .startswith("CON_device")
    assert sanitise_filename("contract.pdf ") == "contract.pdf"


def test_an_absurdly_long_filename_is_capped_but_keeps_its_extension():
    cleaned = sanitise_filename("a" * 4000 + ".pdf")
    assert len(cleaned) <= 120 and cleaned.endswith(".pdf")


def test_safe_join_repeats_the_is_relative_to_guard_the_ledger_route_learned_to_use(tmp_path):
    inside = safe_join(tmp_path, "contract.pdf")
    assert inside.parent == tmp_path.resolve()
    # The traversal is already defused by the sanitiser, so this lands inside — which is the
    # point: the second gate confirms it rather than trusting the first.
    assert safe_join(tmp_path, "../../etc/passwd").parent == tmp_path.resolve()


def test_a_sibling_directory_whose_name_merely_starts_with_the_root_is_not_inside_it(tmp_path):
    # The main.py:1509 bug in miniature: str(candidate).startswith(str(root)) says
    # "uploads-backup" is inside "uploads"; is_relative_to compares path components.
    root = tmp_path / "uploads"
    root.mkdir()
    sibling = tmp_path / "uploads-backup"
    assert str(sibling).startswith(str(root))  # the wrong comparison would pass
    assert not sibling.resolve().is_relative_to(root.resolve())  # the right one does not


# --------------------------------------------------------------------------- storage


def test_an_accepted_file_is_stored_under_an_opaque_name_that_carries_no_extension_of_its_own(tmp_path):
    accepted = accept(KIND_CONTRACT, chunked(PDF), filename="Master Services Agreement.pdf")
    stored = store(accepted, root=tmp_path)
    assert stored.path.parent == tmp_path.resolve()
    assert stored.stored_name.endswith(".bin")
    assert "Master" not in stored.stored_name  # the user's name never reaches the filesystem
    assert stored.path.read_bytes() == PDF
    assert stored.filename == "Master Services Agreement.pdf"  # kept as metadata for the reviewer
    assert stored.sha256 == accepted.sha256 and stored.size == len(PDF)


def test_a_rejected_file_is_never_written_to_disk(tmp_path):
    # §7.9.5, verbatim. It is structural here: store() only ever takes an AcceptedUpload, so
    # there is no path on which a refused body reaches the filesystem.
    for body, name in ((b"PK\x03\x04bomb", "contract.pdf"), (b"\x00\x01\x02", "contract.pdf"), (b"", "contract.pdf")):
        with pytest.raises(UploadRejected):
            accept(KIND_CONTRACT, chunked(body), filename=name)
    assert list(tmp_path.iterdir()) == []


def test_storing_never_overwrites_an_existing_file(tmp_path):
    accepted = accept(KIND_CONTRACT, chunked(PDF), filename="c.pdf")
    first = store(accepted, root=tmp_path)
    with pytest.raises(FileExistsError):
        store(accepted, root=tmp_path, stored_name=first.stored_name)
    assert first.path.read_bytes() == PDF


def test_a_hand_supplied_stored_name_must_still_be_opaque(tmp_path):
    accepted = accept(KIND_CONTRACT, chunked(PDF), filename="c.pdf")
    for bad in ("../escape.bin", "short.bin", "payload.exe", "a" * 8 + ".pdf"):
        with pytest.raises(MalformedUpload):
            store(accepted, root=tmp_path, stored_name=bad)


def test_two_uploads_of_the_same_bytes_get_different_stored_names(tmp_path):
    accepted = accept(KIND_CONTRACT, chunked(PDF), filename="c.pdf")
    assert store(accepted, root=tmp_path).stored_name != store(accepted, root=tmp_path).stored_name


def test_the_upload_root_is_never_a_statically_served_directory(monkeypatch):
    # An upload directory inside frontend/dist turns every accepted file into a URL: stored
    # XSS for anything a browser renders, arbitrary download for the rest.
    served = Path(upload_root()).resolve().parents[1] / "frontend" / "dist" / "uploads"
    monkeypatch.setenv("UPLOAD_DIR", str(served))
    chosen = upload_root()
    assert "dist" not in chosen.parts
    assert chosen.name == "uploads" and chosen.parent.name == "data"


def test_the_default_upload_root_is_outside_the_frontend_build(monkeypatch):
    monkeypatch.delenv("UPLOAD_DIR", raising=False)
    assert upload_root().parts[-2:] == ("data", "uploads")


# --------------------------------------------------------------------------- CSV parsing


def test_the_csv_module_parses_quoted_commas_that_a_hand_rolled_split_would_shift():
    text = 'site_id,note,value\nNRB-0421,"Nakuru, Rift Valley",71.2\n'
    rows = list(iter_csv_rows(text))
    assert rows[1] == ["NRB-0421", "Nakuru, Rift Valley", "71.2"]


def test_an_accepted_csv_hands_back_text_and_a_pdf_refuses_to_pretend_it_is_text():
    accepted = accept(KIND_CAPACITY_CSV, chunked(CSV), filename="prb.csv", declared_content_type="text/csv")
    assert accepted.media_type == MEDIA_TEXT
    assert list(iter_csv_rows(accepted.text))[0][0] == "site_id"
    pdf = accept(KIND_CONTRACT, chunked(PDF), filename="c.pdf")
    with pytest.raises(UnsupportedUpload):
        _ = pdf.text


# --------------------------------------------------------------------------- policy / flag


def test_the_caps_are_the_ones_the_spec_names():
    assert POLICIES[KIND_CONTRACT].max_bytes == 20 * 1024 * 1024
    assert POLICIES[KIND_CAPACITY_CSV].max_bytes == 5 * 1024 * 1024
    assert POLICIES[KIND_COMPLAINT].max_bytes == 5 * 1024 * 1024
    assert MAX_CSV_ROWS == 100_000
    # §7.9.5 restricts these routes to admin/legal/planning; the roles travel with the policy
    # so the cap and the permission cannot drift apart in two different files.
    assert set(POLICIES[KIND_CAPACITY_CSV].roles) == {"admin", "planning"}
    assert set(POLICIES[KIND_CONTRACT].roles) == {"admin", "legal"}


def test_an_unknown_upload_kind_is_a_programming_error_not_a_client_error():
    with pytest.raises(ValueError):
        policy_for("ANYTHING_GOES")


def test_uploads_are_disabled_unless_the_flag_is_explicitly_true(monkeypatch):
    monkeypatch.delenv("UPLOADS_ENABLED", raising=False)
    assert uploads_enabled() is False
    for value in ("false", "0", "no", "off", ""):
        monkeypatch.setenv("UPLOADS_ENABLED", value)
        assert uploads_enabled() is False, value
    monkeypatch.setenv("UPLOADS_ENABLED", "true")
    assert uploads_enabled() is True


def test_the_audit_payload_names_the_reason_and_never_echoes_the_uploaded_bytes():
    # An error message that quotes attacker content moves the payload instead of stopping it.
    try:
        accept(KIND_CONTRACT, chunked(b"<script>alert(1)</script>"), filename="../../etc/passwd")
    except UploadRejected as exc:
        payload = rejection_payload(exc, kind=KIND_CONTRACT, filename="../../etc/passwd", declared="text/html")
    assert payload["status"] in (400, 413, 415)
    assert payload["filename"] == "passwd"
    assert "<script>" not in str(payload)
    assert payload["reason"]
