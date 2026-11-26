"""How a lint over the repository's own text reads a file and spells a name, one rule for every such lint.

The lints that refuse a name (`lakehouse_names_no_client.py`, `code_names_no_reference_product.py`,
`the_lakehouse_runs_no_dapr.py`) judge what a file SAYS, so a byte that is not UTF-8, an invisible
character, a letter from another script that looks Latin, or a name hyphenated or wrapped over two comment lines must
not hide a name from them. A file is read as text whatever its bytes (an undecodable byte becomes U+FFFD and the rest
is still judged), folded to one spelling (NFKC, format characters such as a zero-width space removed, look-alike
Cyrillic and Greek letters mapped to the Latin they imitate), and a name is matched with a joiner allowed between its
letters.
"""

import re
import unicodedata
from pathlib import Path


#: Cyrillic and Greek letters NFKC keeps apart from the Latin letters they are drawn like.
LOOKALIKES = str.maketrans(
    "аАВеЕіІјЈКМНоОрРсСТуХхѕЅԁһӏαΑΒεΕηΗιΙκΚΜνΝοΟρΡτΤυΥχΧ",
    "aABeEiIjJKMHoOpPcCTyXxsSdhlaABeEnHiIkKMvNoOpPtTuYxX",
)
#: What may stand between two letters of a name and leave it the same name: a hyphen, an underscore or a dot, or a line
#: break with the next line's comment marker, so `Some-name` and a name wrapped over two comment lines are read whole.
JOINER = r"(?:[-_.]|-?[ \t]*\r?\n[ \t]*(?:#+|//+|/?\*+|--|;+)?[ \t]*)?"


def judged_text(path: Path) -> str | None:
    """The file's text folded to the spelling a name is matched in, ``None`` when it is listed but absent."""
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None
    text = unicodedata.normalize("NFKC", raw.decode("utf-8", errors="replace"))
    return "".join(char for char in text if unicodedata.category(char) != "Cf").translate(LOOKALIKES)


def spelled(name: str) -> str:
    """A pattern for ``name`` with `JOINER` allowed between any two of its letters."""
    return JOINER.join(re.escape(char) for char in name if char.isalnum())


def line_hits(name: str, text: str, found: re.Pattern[str]) -> list[str]:
    """Each match of ``found`` in ``text`` as ``<name>:<line>: <match>``, the line the match starts on."""
    return [f"{name}:{text.count(chr(10), 0, match.start()) + 1}: {match.group(0)!r}" for match in found.finditer(text)]
