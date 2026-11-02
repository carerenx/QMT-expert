"""Translate ETF option execution fields to MiniQMT order types (Python 3.6)."""


def option_order_type(direction, offset_flag, action=None):
    """Return 48/49/50/51, or 0 when an option action is not identifiable.

    BigQMT direction: 48 buy, 49 sell; offset: 48 open, 49 close.
    Do not confuse these with BigQMT passorder opTypes (50 through 53).
    Only absent direction may fall back to the normalized BUY/SELL action.
    """
    if direction is None or direction == "":
        direction = {"BUY": 48, "SELL": 49}.get(str(action or "").upper())
    try:
        # Reject booleans and fractional values instead of truncating enums.
        if isinstance(direction, bool) or isinstance(offset_flag, bool):
            return 0
        side, offset = int(direction), int(offset_flag)
        if str(direction) != str(side) or str(offset_flag) != str(offset):
            return 0
    except (TypeError, ValueError, OverflowError):
        return 0
    return {(48, 48): 48, (49, 49): 49,
            (49, 48): 50, (48, 49): 51}.get((side, offset), 0)
