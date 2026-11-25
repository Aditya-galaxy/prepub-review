"""A small mmCIF reader, enough to check a deposited file against its deposition record.

This is not a general CIF parser and does not try to be. It reads the first data block of an
mmCIF file and returns the handful of items a deposition system needs to confirm that the file
someone uploaded is the file they said they were uploading: the entry id, the title, the
experimental method, the resolution and the audit authors with their ORCID iDs.

What it does handle, because real archive files contain all of it:
  - single and double quoted values, including apostrophes inside double quotes
  - semicolon text fields, which are the only way mmCIF carries a multi-line value
  - `loop_` constructs, which is how authors and experimental methods are stored
  - the `.` and `?` placeholders, which mean "not applicable" and "unknown" and are not data
  - comments, which begin with # but only where a token may begin

What it does not handle, and would need a real CIF library for: save frames, multiple data
blocks, and the CIF 2.0 syntax. A deposition file uses none of these, and pretending otherwise
would be worse than saying so.
"""
from dataclasses import dataclass, field

MISSING = {".", "?"}


class MMCIFError(ValueError):
    pass


def _tokens(text):
    """Yield (is_tag, value) for the first data block.

    Semicolon fields are recognised only at the start of a line, which is what the format
    requires and is what stops a semicolon inside a title from ending the value early.
    """
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]

        if line.startswith(";"):
            # A semicolon text field runs to the next line that is just a semicolon.
            chunk = [line[1:]]
            i += 1
            while i < len(lines) and not lines[i].startswith(";"):
                chunk.append(lines[i])
                i += 1
            if i >= len(lines):
                raise MMCIFError("unterminated semicolon text field")
            i += 1
            yield False, "\n".join(chunk).strip("\n")
            continue

        pos = 0
        while pos < len(line):
            char = line[pos]
            if char in " \t":
                pos += 1
                continue
            if char == "#":
                break                                   # comment to end of line
            if char in "'\"":
                end = line.find(char, pos + 1)
                # A quote only closes the value when whitespace or end-of-line follows it, so an
                # apostrophe inside a name such as 'O'Brien, S.' does not truncate it.
                while end != -1 and end + 1 < len(line) and line[end + 1] not in " \t":
                    end = line.find(char, end + 1)
                if end == -1:
                    raise MMCIFError(f"unterminated quoted value: {line[pos:][:40]}")
                yield False, line[pos + 1:end]
                pos = end + 1
                continue
            end = pos
            while end < len(line) and line[end] not in " \t":
                end += 1
            word = line[pos:end]
            pos = end
            yield word.startswith("_"), word
        i += 1


def parse(text):
    """Return (items, loops) for the first data block.

    `items` maps "_category.item" to a value. `loops` maps a category name to a list of row
    dicts keyed by item name.
    """
    if "data_" not in text:
        raise MMCIFError("no data block found; this does not look like an mmCIF file")

    items, loops = {}, {}
    stream = list(_tokens(text))
    i = 0
    while i < len(stream):
        is_tag, value = stream[i]

        if not is_tag and value.lower() == "loop_":
            i += 1
            tags = []
            while i < len(stream) and stream[i][0]:
                tags.append(stream[i][1])
                i += 1
            if not tags:
                raise MMCIFError("loop_ with no tags")
            rows = []
            while i < len(stream) and not stream[i][0] and stream[i][1].lower() != "loop_":
                row = {}
                for tag in tags:
                    if i >= len(stream) or stream[i][0]:
                        raise MMCIFError(f"loop_ row ended early at {tag}")
                    row[tag.split(".", 1)[-1]] = _clean(stream[i][1])
                    i += 1
                rows.append(row)
            category = tags[0].split(".", 1)[0].lstrip("_")
            loops.setdefault(category, []).extend(rows)
            continue

        if is_tag:
            if i + 1 < len(stream) and not stream[i + 1][0]:
                items[value] = _clean(stream[i + 1][1])
                i += 2
                continue
            raise MMCIFError(f"tag {value} has no value")

        i += 1

    return items, loops


def _clean(value):
    return None if value in MISSING else value


@dataclass
class Header:
    entry_id: str = None
    title: str = None
    method: str = None
    resolution: float = None
    authors: list = field(default_factory=list)          # [{"name": ..., "orcid": ...}]


def header(text):
    """Pull the items a deposition record can be checked against."""
    items, loops = parse(text)

    method = items.get("_exptl.method")
    if method is None and loops.get("exptl"):
        method = loops["exptl"][0].get("method")

    resolution = None
    for key in ("_refine.ls_d_res_high", "_em_3d_reconstruction.resolution",
                "_reflns.d_resolution_high"):
        if items.get(key) is not None:
            try:
                resolution = float(items[key])
            except ValueError:
                resolution = None
            break

    authors = [{"name": row.get("name"), "orcid": row.get("identifier_ORCID")}
               for row in loops.get("audit_author", [])]
    if not authors and items.get("_audit_author.name"):
        authors = [{"name": items["_audit_author.name"],
                    "orcid": items.get("_audit_author.identifier_ORCID")}]

    return Header(
        entry_id=items.get("_entry.id"),
        title=items.get("_struct.title"),
        method=method.upper() if method else None,
        resolution=resolution,
        authors=authors,
    )


def _norm(value):
    return " ".join(str(value).split()).casefold() if value is not None else None


def discrepancies(deposition, text, resolution_tolerance=0.05):
    """Compare a deposited file's header against the deposition record.

    This is the check a curator needs: does the file say what the record says? A mismatch is not
    necessarily fraud, and is usually a re-upload of the wrong file or a title edited in one place
    and not the other, but it has to be caught before release rather than after.

    Returns a list of (field, in_record, in_file). Empty means the two agree.
    """
    found = header(text)
    out = []

    if found.entry_id and _norm(found.entry_id) != _norm(deposition.accession):
        out.append(("accession", deposition.accession, found.entry_id))

    if found.title and _norm(found.title) != _norm(deposition.title):
        out.append(("title", deposition.title, found.title))

    if found.method and _norm(found.method) != _norm(deposition.method):
        out.append(("method", deposition.method, found.method))

    if found.resolution is not None and deposition.resolution is not None:
        if abs(found.resolution - deposition.resolution) > resolution_tolerance:
            out.append(("resolution", deposition.resolution, found.resolution))
    elif found.resolution is not None and deposition.resolution is None:
        out.append(("resolution", None, found.resolution))

    # Only checked when the record has an iD to check against. An entry whose authors list no
    # ORCID iD is normal and is not a discrepancy.
    recorded = deposition.depositor.orcid
    if recorded:
        in_file = {a["orcid"] for a in found.authors if a["orcid"]}
        if in_file and recorded not in in_file:
            out.append(("depositor ORCID", recorded, ", ".join(sorted(in_file))))

    return out
