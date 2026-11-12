"""Word validation + data loading for the .blacktea game.

Kept separate from cogs/blacktea.py the same way database.py and
imaging.py are kept separate from their cogs - plain, testable
functions with no discord.py objects involved.
"""
from typing import Optional


def load_dictionary(path: str) -> set:
    """One word per line, case-insensitive. Lines starting with '#'
    and blank lines are skipped. Missing file -> empty set (the cog
    checks for this and refuses to start a game rather than silently
    accepting everything)."""
    words = set()
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip().lower()
                if line and not line.startswith("#"):
                    words.add(line)
    except FileNotFoundError:
        pass
    return words


def load_combos(path: str) -> list:
    """One 3-letter combo per line (case-insensitive, normalized to
    upper for display). '#' comment lines and blanks are skipped;
    anything that isn't exactly 3 alphabetic characters is skipped
    too, so a stray typo in the file can't produce an unplayable
    round."""
    combos = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                combo = line.upper()
                if len(combo) == 3 and combo.isalpha():
                    combos.append(combo)
    except FileNotFoundError:
        pass
    return combos


def check_answer(content: str, combo: str, dictionary: set, used_words: set) -> Optional[str]:
    """Returns the normalized (lowercase) word if `content` is a legal
    answer for this turn, else None. Every failure mode - wrong
    person, wrong combo, not a real word, already used, more than one
    word, empty message - is deliberately collapsed into the same
    "not valid" result: the caller (a discord.py wait_for check)
    treats None as "keep waiting", which is exactly how the original
    ask wants a wrong answer handled - silently ignored, no reaction,
    no penalty, round just keeps listening.
    """
    word = content.strip().lower()
    if not word or not word.isalpha():
        return None  # not exactly one plain word
    if combo.lower() not in word:
        return None
    if word not in dictionary:
        return None
    if word in used_words:
        return None
    return word
