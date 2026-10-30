from meeting_notes.capture_plan import (
    OutputEndpoint,
    describe_route_change,
    mark_default_loopback,
    note_output_misses,
    outputs_to_open,
    snapshot_route,
    sum_aligned,
    SilenceTracker,
    take_span,
)


def test_exact_loopback_name_beats_a_shorter_substring():
    names = ["Speakers (HD) [Loopback]", "Speakers [Loopback]"]
    assert mark_default_loopback("Speakers", names) == "Speakers [Loopback]"
    assert mark_default_loopback("Headphones", ["Speakers [Loopback]", "Headphones [Loopback]"]) == (
        "Headphones [Loopback]"
    )
    assert mark_default_loopback(None, names) is None
    assert mark_default_loopback("HDMI", names) is None


def test_new_headphones_stay_alongside_the_old_output():
    """Default can move to a new endpoint without dropping the one already open."""
    previous = snapshot_route("Laptop mic", [OutputEndpoint("Speakers [Loopback]", True)])
    current = snapshot_route(
        "Laptop mic",
        [
            OutputEndpoint("Speakers [Loopback]", False),
            OutputEndpoint("Headphones [Loopback]", True),
        ],
    )
    lines = describe_route_change(previous, current)
    assert "output added: 'Headphones [Loopback]' (default output)" in lines
    assert "default output switched: 'Speakers [Loopback]' -> 'Headphones [Loopback]'" in lines
    assert not any(line.startswith("output removed") for line in lines)
    assert outputs_to_open(
        [endpoint.name for endpoint in previous.outputs],
        [endpoint.name for endpoint in current.outputs],
    ) == ["Headphones [Loopback]"]


def test_non_default_output_is_captured_and_not_closed():
    route = snapshot_route(
        "Laptop mic",
        [
            OutputEndpoint("Headphones [Loopback]", False),
            OutputEndpoint("Speakers [Loopback]", True),
        ],
    )
    lines = describe_route_change(None, route)
    assert "capturing microphone 'Laptop mic'" in lines
    assert "capturing default output 'Speakers [Loopback]'" in lines
    assert "capturing non-default output 'Headphones [Loopback]'" in lines
    names = [endpoint.name for endpoint in route.outputs]
    assert outputs_to_open(names, names) == []


def test_default_switch_between_devices_already_open_does_not_restart_them():
    previous = snapshot_route(
        "Mic",
        [
            OutputEndpoint("Headphones [Loopback]", False),
            OutputEndpoint("Speakers [Loopback]", True),
        ],
    )
    current = snapshot_route(
        "Mic",
        [
            OutputEndpoint("Headphones [Loopback]", True),
            OutputEndpoint("Speakers [Loopback]", False),
        ],
    )
    lines = describe_route_change(previous, current)
    assert lines == [
        "default output switched: 'Speakers [Loopback]' -> 'Headphones [Loopback]'"
    ]
    names = [endpoint.name for endpoint in current.outputs]
    assert outputs_to_open(names, names) == []


def test_microphone_switch_is_logged():
    previous = snapshot_route("Laptop mic", [OutputEndpoint("Speakers [Loopback]", True)])
    current = snapshot_route("Headset mic", [OutputEndpoint("Speakers [Loopback]", True)])
    assert describe_route_change(previous, current) == [
        "microphone switched: 'Laptop mic' -> 'Headset mic'"
    ]


def test_output_is_stopped_only_after_two_missed_scans():
    misses, stop = note_output_misses({}, ["Speakers [Loopback]"], ["Headphones [Loopback]"])
    assert stop == []
    assert misses == {"Speakers [Loopback]": 1}
    misses, stop = note_output_misses(misses, ["Speakers [Loopback]"], ["Headphones [Loopback]"])
    assert stop == ["Speakers [Loopback]"]
    assert misses == {}
    # Back on the next scan: the miss count does not carry over.
    misses, stop = note_output_misses(
        {"Speakers [Loopback]": 1},
        ["Speakers [Loopback]"],
        ["Speakers [Loopback]"],
    )
    assert misses == {}
    assert stop == []


def test_short_device_buffer_is_silence_not_a_shift():
    assert take_span(1, 4) == (1, 3)
    mixed, remainders = sum_aligned([[1.0, 2.0, 3.0], [4.0]], 2)
    assert mixed == [5.0, 2.0]
    assert remainders == [[3.0], []]
    # The leftover sample is the next block, not inserted in front of the pad.
    mixed, remainders = sum_aligned(remainders, 2)
    assert mixed == [3.0, 0.0]
    assert remainders == [[], []]


def test_silence_while_mic_is_active_warns_once_then_reminds():
    tracker = SilenceTracker(warn_after_s=60, remind_every_s=300, mic_threshold=0.02, speaker_threshold=0.005)
    assert tracker.observe_window(0.0, 0.0, 1) is None
    for _ in range(59):
        assert tracker.observe_window(0.05, 0.0, 1) is None
    assert tracker.observe_window(0.05, 0.0, 1) == "warn"
    # A quiet moment in the user's own voice pauses the clock; it does not reset it.
    assert tracker.observe_window(0.01, 0.0, 1) is None
    assert tracker.observe_window(0.05, 0.0, 1) is None
    for _ in range(298):
        assert tracker.observe_window(0.05, 0.0, 1) is None
    assert tracker.observe_window(0.05, 0.0, 1) == "remind"
    assert tracker.observe_window(0.05, 0.02, 1) == "recovered"
    assert tracker.observe_window(0.05, 0.0, 1) is None


def test_system_audio_before_the_warning_resets_the_streak():
    tracker = SilenceTracker(warn_after_s=10, remind_every_s=100)
    for _ in range(9):
        assert tracker.observe_window(0.05, 0.0, 1) is None
    assert tracker.observe_window(0.05, 0.01, 1) is None
    for _ in range(9):
        assert tracker.observe_window(0.05, 0.0, 1) is None
    assert tracker.observe_window(0.05, 0.0, 1) == "warn"
