"""Text-to-key mapping for scan-mode typing.

Conservative and explicit rather than clever. Scan-mode typing is only needed for text
entry into surfaces that read raw scancodes, which in practice means a small set of
prompts. Anything outside this table raises instead of guessing, because a wrong guess
types the wrong characters into a field the user is watching.

Uppercase and symbol handling follows the US layout, which is the layout the developer
machine uses. A profile on another layout should bind intents rather than rely on
TypeText (which is classified C3 external-effect precisely because it lands somewhere).
"""

from __future__ import annotations

from frameforge.ports.input import Key

#: Unshifted character -> key.
_UNSHIFTED: dict[str, Key] = {
    " ": Key.SPACE, "\t": Key.TAB, "\n": Key.ENTER,
    "a": Key.A, "b": Key.B, "c": Key.C, "d": Key.D, "e": Key.E, "f": Key.F,
    "g": Key.G, "h": Key.H, "i": Key.I, "j": Key.J, "k": Key.K, "l": Key.L,
    "m": Key.M, "n": Key.N, "o": Key.O, "p": Key.P, "q": Key.Q, "r": Key.R,
    "s": Key.S, "t": Key.T, "u": Key.U, "v": Key.V, "w": Key.W, "x": Key.X,
    "y": Key.Y, "z": Key.Z,
    "0": Key.N0, "1": Key.N1, "2": Key.N2, "3": Key.N3, "4": Key.N4,
    "5": Key.N5, "6": Key.N6, "7": Key.N7, "8": Key.N8, "9": Key.N9,
    "-": Key.MINUS, "=": Key.EQUALS, "[": Key.LBRACKET, "]": Key.RBRACKET,
    "\\": Key.BACKSLASH, ";": Key.SEMICOLON, "'": Key.APOSTROPHE,
    ",": Key.COMMA, ".": Key.PERIOD, "/": Key.SLASH, "`": Key.GRAVE,
}

#: Shifted character -> key.
_SHIFTED: dict[str, Key] = {
    "A": Key.A, "B": Key.B, "C": Key.C, "D": Key.D, "E": Key.E, "F": Key.F,
    "G": Key.G, "H": Key.H, "I": Key.I, "J": Key.J, "K": Key.K, "L": Key.L,
    "M": Key.M, "N": Key.N, "O": Key.O, "P": Key.P, "Q": Key.Q, "R": Key.R,
    "S": Key.S, "T": Key.T, "U": Key.U, "V": Key.V, "W": Key.W, "X": Key.X,
    "Y": Key.Y, "Z": Key.Z,
    "!": Key.N1, "@": Key.N2, "#": Key.N3, "$": Key.N4, "%": Key.N5,
    "^": Key.N6, "&": Key.N7, "*": Key.N8, "(": Key.N9, ")": Key.N0,
    "_": Key.MINUS, "+": Key.EQUALS, "{": Key.LBRACKET, "}": Key.RBRACKET,
    "|": Key.BACKSLASH, ":": Key.SEMICOLON, '"': Key.APOSTROPHE,
    "<": Key.COMMA, ">": Key.PERIOD, "?": Key.SLASH, "~": Key.GRAVE,
}


def char_to_key(ch: str) -> tuple[Key, bool]:
    """Return ``(key, needs_shift)`` for one character."""
    if ch in _UNSHIFTED:
        return _UNSHIFTED[ch], False
    if ch in _SHIFTED:
        return _SHIFTED[ch], True
    msg = f"character {ch!r} is not in the scan-mode map; use method='unicode' or bind an intent"
    raise ValueError(msg)


def text_to_keys(text: str) -> list[Key]:
    """Convert text to a key sequence, expanding shifted characters with Shift.

    Shift is inserted as its own key so the executor's hotkey-style balancing keeps the
    modifier released. This is a simplification: a true implementation would hold Shift
    across a run of shifted characters for realistic timing, which matters only if a
    target counts keystrokes - which is an evasion concern and explicitly out of scope
    (guardrail G-ABS-03). Correct release semantics is what we actually need.
    """
    out: list[Key] = []
    for ch in text:
        key, needs_shift = char_to_key(ch)
        if needs_shift:
            out.append(Key.LSHIFT)
        out.append(key)
        if needs_shift:
            out.append(Key.LSHIFT)
    return out


def is_typable(text: str) -> bool:
    return all(c in _UNSHIFTED or c in _SHIFTED for c in text)


__all__ = ["char_to_key", "is_typable", "text_to_keys"]
