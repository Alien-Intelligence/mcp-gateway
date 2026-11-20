from mcp_gateway.sessions import SessionRegistry, is_valid_session_id


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def make(
    max_entries: int = 3, ttl: float = 60.0, per_subject: int | None = None
) -> tuple[SessionRegistry, Clock]:
    clock = Clock()
    registry = SessionRegistry(
        max_entries=max_entries,
        max_entries_per_subject=max_entries if per_subject is None else per_subject,
        idle_ttl_seconds=ttl,
        clock=clock,
    )
    return registry, clock


def test_bound_session_is_usable_by_its_subject_only() -> None:
    registry, _ = make()
    assert registry.bind("up", "s1", "alice")
    assert registry.check("up", "s1", "alice")
    assert not registry.check("up", "s1", "bob")


def test_unknown_session_and_other_upstream_are_refused() -> None:
    registry, _ = make()
    registry.bind("up", "s1", "alice")
    assert not registry.check("up", "nope", "alice")
    assert not registry.check("other", "s1", "alice")


def test_idle_ttl_expires_and_use_refreshes() -> None:
    registry, clock = make(ttl=60)
    registry.bind("up", "s1", "alice")
    clock.now += 50
    assert registry.check("up", "s1", "alice")  # refreshed at t+50
    clock.now += 50
    assert registry.check("up", "s1", "alice")  # 50 s idle < ttl
    clock.now += 61
    assert not registry.check("up", "s1", "alice")
    assert len(registry) == 0


def test_touch_refreshes_without_checking_the_owner() -> None:
    registry, clock = make(ttl=60)
    registry.bind("up", "s1", "alice")
    clock.now += 50
    registry.touch("up", "s1")
    clock.now += 50
    assert registry.check("up", "s1", "alice")


def test_drop_removes_the_binding() -> None:
    registry, _ = make()
    registry.bind("up", "s1", "alice")
    registry.drop("up", "s1")
    registry.drop("up", "never-bound")
    assert not registry.check("up", "s1", "alice")


def test_registry_is_bounded_and_evicts_least_recently_used() -> None:
    registry, clock = make(max_entries=2)
    registry.bind("up", "s1", "alice")
    clock.now += 1
    registry.bind("up", "s2", "alice")
    clock.now += 1
    assert registry.check("up", "s1", "alice")  # s2 is now the oldest
    registry.bind("up", "s3", "alice")
    assert len(registry) == 2
    assert registry.check("up", "s1", "alice")
    assert registry.check("up", "s3", "alice")
    assert not registry.check("up", "s2", "alice")


def test_expired_entries_are_dropped_before_live_ones_are_evicted() -> None:
    registry, clock = make(max_entries=2, ttl=60)
    registry.bind("up", "old", "alice")
    clock.now += 30
    registry.bind("up", "live", "alice")
    clock.now += 40  # old is 70 s idle, live 40 s
    registry.bind("up", "new", "alice")
    assert not registry.check("up", "old", "alice")
    assert registry.check("up", "live", "alice")
    assert registry.check("up", "new", "alice")


def test_an_id_bound_to_someone_else_is_never_moved() -> None:
    registry, _ = make()
    assert registry.bind("up", "s1", "alice")
    assert not registry.bind("up", "s1", "bob")
    assert registry.check("up", "s1", "alice")
    assert not registry.check("up", "s1", "bob")


def test_rebinding_by_the_same_subject_is_fine() -> None:
    registry, _ = make()
    assert registry.bind("up", "s1", "alice")
    assert registry.bind("up", "s1", "alice")


def test_malformed_ids_are_not_bound() -> None:
    registry, _ = make()
    assert not registry.bind("up", "has space", "alice")
    assert not registry.bind("up", "", "alice")
    assert not registry.bind("up", "x" * 513, "alice")
    assert len(registry) == 0
    assert is_valid_session_id("1868a90c-AbC~!")


def test_a_subject_over_its_cap_evicts_only_its_own_oldest_binding() -> None:
    registry, clock = make(max_entries=10, per_subject=2)
    registry.bind("up", "bob-1", "bob")
    for index in range(5):
        clock.now += 1
        registry.bind("up", f"alice-{index}", "alice")
    assert registry.check("up", "bob-1", "bob"), "another subject's binding is never evicted"
    assert [registry.check("up", f"alice-{i}", "alice") for i in range(5)] == [
        False,
        False,
        False,
        True,
        True,
    ]
    assert len(registry) == 3


def test_rebinding_a_known_session_does_not_count_twice() -> None:
    registry, _ = make(max_entries=10, per_subject=2)
    registry.bind("up", "a1", "alice")
    registry.bind("up", "a2", "alice")
    assert registry.bind("up", "a2", "alice")
    assert registry.check("up", "a1", "alice")


def test_dropped_and_expired_bindings_free_the_subject_cap() -> None:
    registry, clock = make(max_entries=10, ttl=60, per_subject=2)
    registry.bind("up", "a1", "alice")
    registry.bind("up", "a2", "alice")
    registry.drop("up", "a1")
    registry.bind("up", "a3", "alice")
    assert registry.check("up", "a2", "alice") and registry.check("up", "a3", "alice")
    clock.now += 61
    assert not registry.check("up", "a2", "alice")
    registry.bind("up", "a4", "alice")
    registry.bind("up", "a5", "alice")
    assert registry.check("up", "a4", "alice") and registry.check("up", "a5", "alice")
