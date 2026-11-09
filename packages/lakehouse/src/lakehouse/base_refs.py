"""One rule for comparing the locations a manifest's ``base_paths`` and the catalog's records name ([[LH-279]], [[LH-318]]).

A location reaches the estate in more than one spelling: ``describe_table`` answers a registered location
percent-encoded, while the store keeps, and discovery lists, the directory that spelling opens. Every reader that compares
two locations, a base with a table root, a claim with a trash record, an event with a listing, asks the helpers here, so
two spellings of one path are never two places: a comparator that silently never matches looks exactly like no guard at
all.
"""

from __future__ import annotations

from urllib.parse import unquote_to_bytes


def normalise(uri: str) -> str:
    """Compare paths, not spellings: drop the scheme and any trailing slash.

    A manifest states ``/bucket/ns/t.lance`` where the caller holds ``s3://bucket/ns/t.lance``. Left
    unnormalised the guard silently never matches, which is the failure mode that looks exactly like
    having no guard at all.

    The LEADING slash is stripped for the same reason and it is the half that is easy to miss:
    dropping the scheme from ``s3://bucket/x`` leaves ``bucket/x`` while the manifest for the same
    object says ``/bucket/x``, so scheme-stripping alone still fails to match. Two spellings of one
    path must compare equal, and no two different paths are conflated by this — a leading slash never
    distinguishes one object from another here.
    """
    without_scheme = uri.split("://", 1)[-1]
    return without_scheme.strip("/")


#: PUBLIC because the catalog and maintenance compare spellings of one store's paths with it. It drops
#: the scheme and the edge slashes and decodes nothing, so two spellings of one path that differ in
#: percent-encoding compare equal only as :func:`decoded_path`, the form every base-reference verdict
#: here and the sweep's trash exclusion compare and key in ([[LH-318]]).
#: Re-implementing the compare at the call site is how a guard silently never matches, which this
#: function's own docstring names as the failure mode indistinguishable from having no guard at all.
#: The private alias stays so the in-module call sites read unchanged.
_normalise = normalise

#: Path segments that name no object of their own. `..` is the one that matters: pylance 12.0.0
#: resolves ``<attacker>/../victim`` on a local root, and the attacker then reads the victim's rows
#: (lh279 s4) — a base textually UNDER the attacker's root that is not. `.` and the empty segment are
#: refused with it because a comparison on the spelling cannot say where they resolve either.
_NON_LOCATION_SEGMENTS = frozenset({"", ".", ".."})

#: Characters that can divide a path into segments: `/` in every store, and `\\` in a ``file://`` URL or
#: on a Windows filesystem. A decode that adds one to a segment can give the opened path a boundary the
#: spelling does not show. pylance 12.0.0 on Linux opens a decoded `\\` as a name character
#: (lh279 r5 escape: ``x/..%5C..%5Cvictim`` reads nothing), so refusing it is the conservative half.
_SEPARATORS = ("/", "\\")

#: A URI's query delimiter. The store does not keep a segment holding one under the name it decodes to: measured on
#: pylance 13.0.0 over the dir backend ([[LH-435]]), a register at ``x?y`` or ``x%3Fy`` keeps the location re-encoded,
#: and the id finds no table on query or count. So a spelling holding one names no location, for every judge alike
#: ([[LH-510]]). A table the catalog makes lives under its uuid, which holds none (ADR 0173 §5), so no location the
#: catalog mints does.
_QUERY_DELIMITER = "?"


def _decoded_once(segment: str) -> str | None:
    """``segment`` percent-decoded once, as pylance decodes it; ``None`` when the bytes are not UTF-8.

    pylance 12.0.0 refuses such a path outright ("contained non-unicode characters", over moto and a local
    root, lh279 r5 not_utf8), so it names no location.
    """
    try:
        return unquote_to_bytes(segment).decode("utf-8")
    except UnicodeDecodeError:
        return None


def decoded_segments(uri: str) -> list[str] | None:
    """The segments of ``uri``'s path as pylance opens them, or ``None`` when it names no location (:func:`names_a_location`)."""
    path = _normalise(uri)
    if not path:
        return None
    segments: list[str] = []
    for spelled in path.split("/"):
        segment = _decoded_once(spelled)
        if segment is None or segment in _NON_LOCATION_SEGMENTS or _QUERY_DELIMITER in segment:
            return None
        if any(ord(char) < 0x20 or ord(char) == 0x7F for char in segment):
            return None
        if any(segment.count(separator) > spelled.count(separator) for separator in _SEPARATORS):
            return None
        segments.append(segment)
    return segments


def decoded_path(uri: str) -> str:
    """The path ``uri`` names, as pylance opens it: its :func:`decoded_segments` joined by ``/``.

    THE ONE FORM a base-reference verdict compares and keys a location in. The estate holds one table in
    two spellings: the catalog percent-encodes the name into the location it records and a branch's
    manifest declares (``…$r%C3%A4ksm%C3%B6rg%C3%A5s``), while the store keeps the decoded directory and
    discovery lists that (``…$räksmörgås``; lh279 r6 spellings). Compared as spelled they never match: a
    branch's base on its parent is judged a finding, and nothing protects the parent (lh279 rvw finding1).

    A spelling that names no location keeps its normalised spelling (:func:`normalise`): no decode says
    where it is, and every predicate here already answers it as naming nothing.
    """
    segments = decoded_segments(uri)
    return _normalise(uri) if segments is None else "/".join(segments)


def names_a_location(uri: str) -> bool:
    """Whether ``uri`` spells one place, so that comparing the path it decodes to says where it is.

    Every containment answer below compares decoded paths (:func:`decoded_path`), and that is an answer
    about the place only when the decode is the one the store performs. pylance 12.0.0 decodes a location
    the way a URL is decoded: it percent-decodes each segment ONCE, resolves the ``.`` and ``..`` segments
    that leaves, and drops a tab, line feed or carriage return (measured over moto and a local root, lh279
    r3 a_escape). So each ``/``-separated segment is judged as it reads after one decode, and the spelling
    names no location when any decoded segment is empty, ``.`` or ``..``, holds an ASCII control
    character, is not UTF-8, holds a ``?`` (:data:`_QUERY_DELIMITER`), or holds a ``/`` or ``\\`` the decode added. That refuses
    ``<own>/%2e%2e/<victim>``, ``<own>/.%2e/<victim>``, ``<own>/.\\t./<victim>`` and a configured
    ``<models>/%2e%2e/<victim>``, each spelled inside a sanctioned prefix while opening another table.

    Any other ``%`` escape names the place it decodes to, and must stay a location: the catalog
    percent-encodes a table's name into its location (``räksmörgås`` -> ``r%C3%A4ksm%C3%B6rg%C3%A5s``,
    ``growth%`` -> ``growth%25``, a space -> ``%20``), and a branch of that table declares its root spelled
    so (lh279 r3b names, r5 manifest). A second level of encoding is a name: ``%252e%252e`` decodes once to the literal
    segment ``%2e%2e``, which pylance opens as a directory of that name (lh279 r5 escape).
    """
    return decoded_segments(uri) is not None


def location_within(outer: str, inner: str) -> bool:
    """Whether ``inner`` is the location ``outer`` names or lies beneath it, segment by decoded segment.

    On a path BOUNDARY: ``bucket/t`` does not contain ``bucket/t-evil``. Each side is read as
    :func:`decoded_segments` reads it, so the catalog's ``…$r%C3%A4ksm%C3%B6rg%C3%A5s`` and the
    ``…$räksmörgås`` discovery lists are one location. Refuses to vouch for either side when it does not
    name a location (:func:`names_a_location`), because ``bucket/t/../victim`` starts with ``bucket/t/``
    and is not under it.
    """
    there, here = decoded_segments(outer), decoded_segments(inner)
    return there is not None and here is not None and here[: len(there)] == there


def store_of(uri: str) -> str:
    """The store a spelled location resolves in: its URI scheme, case-folded, and ``file`` for a bare path.

    :func:`normalise` drops the scheme so two spellings of one path compare equal, which is right only
    inside ONE store. A base resolves through the scheme its own spelling names, not the table's —
    measured on pylance 12.0.0 over moto (lh279fx probe_scheme_resolve): on an S3 table a ``file://`` base
    and a bare ``/path`` base both read the process's local filesystem, and ``S3://`` reads the same store
    as ``s3://``. So a comparison that decides whether a base is a table's own, or an operator's, asks
    this as well as the path.
    """
    scheme, separator, _rest = uri.partition("://")
    return scheme.lower() if separator else "file"


def key_uri(store: str, key: str) -> str:
    """Where the store key ``key`` is listed in the store ``store`` names (:func:`store_of`): an absolute path in ``file``,
    else under the store's scheme. A key is the decoded form a location claim and a base record keep (:func:`decoded_path`)."""
    return f"/{key}" if store == "file" else f"{store}://{key}"


def location_in_store(outer: str, inner: str) -> bool:
    """:func:`location_within`, and only when both spellings name the same store (:func:`store_of`)."""
    return store_of(outer) == store_of(inner) and location_within(outer, inner)
