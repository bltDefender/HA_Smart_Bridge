from smart_bridge.backoff import Backoff


def test_backoff_grows_caps_and_resets() -> None:
    b = Backoff(1, 5)
    assert [b.next_delay() for _ in range(4)] == [1, 2, 4, 5]
    b.reset()
    assert b.next_delay() == 1
