from __future__ import annotations

from collections.abc import Sequence

_CODE_ALPHABET = frozenset("0123456789ABCDEF")
_WIN_LINES = (
    (0, 1, 2),
    (3, 4, 5),
    (6, 7, 8),
    (0, 3, 6),
    (1, 4, 7),
    (2, 5, 8),
    (0, 4, 8),
    (2, 4, 6),
)


def winner(board: Sequence[str | None]) -> int | None:
    for a, b, c in _WIN_LINES:
        if board[a] is not None and board[a] == board[b] == board[c]:
            return 0 if board[a] == "X" else 1
    return None


def board_text(board: Sequence[str | None]) -> str:
    cells = [value or "·" for value in board]
    return "\n".join(" | ".join(cells[row : row + 3]) for row in range(0, 9, 3))


def is_valid_code(code: str) -> bool:
    return len(code) == 6 and all(character in _CODE_ALPHABET for character in code)
