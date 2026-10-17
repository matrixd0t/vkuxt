from examples.tictactoe import CloseInput, MoveInput, board_text, is_valid_code, winner


def test_winner_detects_rows_columns_and_diagonals() -> None:
    assert winner(["X", "X", "X", None, None, None, None, None, None]) == 0
    assert winner(["O", None, None, "O", None, None, "O", None, None]) == 1
    assert winner(["X", None, None, None, "X", None, None, None, "X"]) == 0
    assert winner(["X", "O", "X", "X", "O", "O", "O", "X", "X"]) is None


def test_board_text_renders_empty_cells() -> None:
    assert board_text([None] * 9) == "· | · | ·\n· | · | ·\n· | · | ·"
    assert board_text(["X", None, "O", None, "X", None, None, None, "O"]).splitlines()[
        0
    ] == "X | · | O"


def test_game_code_validation() -> None:
    assert is_valid_code("A1B2C3")
    assert not is_valid_code("A1B2C")
    assert not is_valid_code("A1B2CZ")


def test_example_models_fill_handler_name_by_default() -> None:
    assert CloseInput().handler_name == "ttt.close"
    move = MoveInput(game_id="game", seat=0, cell=4)
    assert move.handler_name == "ttt.move"
    assert move.model_dump()["handler_name"] == "ttt.move"
