"""
One person's one kind of action may start at most once every N seconds.

The dashboard's model-backed buttons spend from one shared gateway key; the role check says who may press
them and this says how often.
"""
from __future__ import annotations

from src.rate_limit import Cooldown


class _Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_the_first_start_is_allowed_and_a_second_inside_the_window_says_how_long_to_wait():
    clock = _Clock()
    cooldown = Cooldown(20, clock=clock)

    assert cooldown.try_start("ann|ask") == 0.0
    clock.now += 5
    assert cooldown.try_start("ann|ask") == 15.0


def test_it_is_allowed_again_once_the_window_has_passed():
    clock = _Clock()
    cooldown = Cooldown(20, clock=clock)
    cooldown.try_start("ann|ask")

    clock.now += 20
    assert cooldown.try_start("ann|ask") == 0.0


def test_a_refused_start_is_not_recorded_so_it_cannot_extend_the_wait():
    clock = _Clock()
    cooldown = Cooldown(20, clock=clock)
    cooldown.try_start("ann|ask")

    clock.now += 10
    cooldown.try_start("ann|ask")                     # refused
    clock.now += 10
    assert cooldown.try_start("ann|ask") == 0.0, "measured from the start that counted, not the refusal"


def test_people_and_kinds_of_action_do_not_share_a_window():
    cooldown = Cooldown(20, clock=_Clock())
    cooldown.try_start("ann|ask")

    assert cooldown.try_start("bob|ask") == 0.0
    assert cooldown.try_start("ann|brief") == 0.0


def test_the_table_stays_bounded():
    clock = _Clock()
    cooldown = Cooldown(20, clock=clock, max_keys=10)
    for n in range(10):
        cooldown.try_start(f"user{n}|ask")

    clock.now += 60                                    # every window has passed
    cooldown.try_start("late|ask")

    assert len(cooldown._started) == 1
