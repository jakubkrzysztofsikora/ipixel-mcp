from ipixel_mcp.display_state import DisplayState, KIND_IDLE, KIND_DISPLAY, KIND_NOTIFY


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def test_starts_idle():
    ds = DisplayState(clock=Clock())
    st = ds.get_display_state()
    assert st["kind"] == KIND_IDLE
    assert st["preempted"] is False


def test_set_base_replaces():
    ds = DisplayState(clock=Clock())
    ds.set_base(owner="a", summary="first")
    ds.set_base(owner="b", summary="second")
    st = ds.get_display_state()
    assert st["owner"] == "b"
    assert st["summary"] == "second"
    assert st["depth"] == 2  # idle floor + one base


def test_preempt_and_restore():
    ds = DisplayState(clock=Clock())
    ds.set_base(owner="disp", summary="base content")
    ds.preempt(owner="agent", summary="[blocked] help", ref_id="n1")
    top = ds.get_display_state()
    assert top["kind"] == KIND_NOTIFY
    assert top["preempted"] is True
    assert top["owner"] == "agent"

    # clearing restores the base underneath
    ds.clear_preempt(ref_id="n1")
    restored = ds.get_display_state()
    assert restored["kind"] == KIND_DISPLAY
    assert restored["summary"] == "base content"


def test_base_inserted_below_active_preempt():
    ds = DisplayState(clock=Clock())
    ds.preempt(owner="agent", summary="urgent", ref_id="n1")
    # a normal display arrives while preempted: it must go UNDER the banner
    ds.set_base(owner="disp", summary="late base")
    assert ds.get_display_state()["kind"] == KIND_NOTIFY  # banner still on top
    ds.clear_preempt(ref_id="n1")
    assert ds.get_display_state()["summary"] == "late base"


def test_clear_unknown_ref_is_noop():
    ds = DisplayState(clock=Clock())
    ds.set_base(owner="d", summary="x")
    assert ds.clear_preempt(ref_id="missing") is None
    assert ds.get_display_state()["summary"] == "x"


def test_ttl_expiry_sweep():
    clk = Clock()
    ds = DisplayState(clock=clk)
    ds.set_base(owner="d", summary="temp", ttl_seconds=10)
    clk.t = 5
    assert ds.get_display_state()["summary"] == "temp"
    assert ds.get_display_state()["ttl_remaining_s"] == 5
    clk.t = 11
    st = ds.get_display_state()
    assert st["kind"] == KIND_IDLE  # expired back to idle


def test_nested_preempt_lifo_restore():
    ds = DisplayState(clock=Clock())
    ds.set_base(owner="d", summary="base")
    ds.preempt(owner="a1", summary="first", ref_id="n1")
    ds.preempt(owner="a2", summary="second", ref_id="n2")
    assert ds.get_display_state()["owner"] == "a2"
    ds.clear_preempt()  # pops top-most (n2)
    assert ds.get_display_state()["owner"] == "a1"
    ds.clear_preempt()
    assert ds.get_display_state()["summary"] == "base"
