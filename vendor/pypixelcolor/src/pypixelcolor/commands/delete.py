from ..lib.transport.send_plan import single_window_plan


def delete(n: int):
    """
    Delete a specific slot by its index.

    Args:
        n: Index of the slot to delete.
    """
    if not (0 <= int(n) <= 255):
        raise ValueError("Slot index must be between 0 and 255")
    cmd = bytes([
        7,      # Command length
        0,      # Reserved
        2,      # Command ID
        1,      # Command type ID
        1,      # Reserved
        0,      # Reserved
        int(n)  # Slot index
    ])
    return single_window_plan("delete_slot", cmd)
