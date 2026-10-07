from lib.alerts.backoff import due, remember

COOLDOWN = 1800


def test_the_first_alert_is_due_at_once_and_a_state_already_alerted_is_not():
    saved = {}
    assert due(saved, "warmer", 1000, COOLDOWN)
    remember(saved, "warmer", 1000)
    assert not due(saved, "warmer", 1000 + 10 * COOLDOWN, COOLDOWN)               # nothing new to say, however long it is


def test_a_change_inside_the_cooldown_waits_and_is_due_when_it_ends_if_it_still_differs():
    saved = {}
    remember(saved, "warmer", 1000)
    assert not due(saved, "cooler", 1000 + COOLDOWN - 1, COOLDOWN)
    assert due(saved, "cooler", 1000 + COOLDOWN, COOLDOWN)
    assert not due(saved, "warmer", 1000 + COOLDOWN, COOLDOWN)                    # back where it was: nothing to say


def test_a_quiet_update_follows_the_state_but_keeps_the_time_of_the_last_real_alert():
    saved = {}
    remember(saved, "dry", 1000)
    remember(saved, "raining", 1500, alerted=False)
    assert saved == {"alerted_state": "raining", "alerted_time": 1000}
    assert due(saved, "dry", 1000 + COOLDOWN, COOLDOWN) and not due(saved, "dry", 1000 + COOLDOWN - 1, COOLDOWN)
