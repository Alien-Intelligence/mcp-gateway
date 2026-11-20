import pytest
from pydantic import SecretStr

from mcp_gateway.identity import Identity
from mcp_gateway.resolver import ChainResolver, Credential, Denied, ResolverError

ID = Identity(subject="u", email=None, organization=None, groups=(), claims={})
KEY = Credential(api_key=SecretStr("fake-alien-key-k"), upstream="primary", labels={})


class Member:
    def __init__(
        self,
        outcome: Credential | Denied | Exception,
        invalidate_error: Exception | None = None,
        close_error: Exception | None = None,
    ) -> None:
        self.outcome = outcome
        self.invalidate_error = invalidate_error
        self.close_error = close_error
        self.calls = 0
        self.invalidated = 0
        self.closed = False

    async def resolve(self, identity: Identity) -> Credential | Denied:
        self.calls += 1
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome

    async def invalidate(self, identity: Identity) -> None:
        self.invalidated += 1
        if self.invalidate_error is not None:
            raise self.invalidate_error

    async def aclose(self) -> None:
        self.closed = True
        if self.close_error is not None:
            raise self.close_error


async def test_no_mapping_moves_on_to_the_next_member() -> None:
    first, second = Member(Denied("no_mapping")), Member(KEY)
    assert await ChainResolver([first, second]).resolve(ID) == KEY
    assert (first.calls, second.calls) == (1, 1)


async def test_first_credential_wins_and_later_members_are_not_asked() -> None:
    first, second = Member(KEY), Member(Denied("no_mapping"))
    assert await ChainResolver([first, second]).resolve(ID) == KEY
    assert second.calls == 0


@pytest.mark.parametrize("reason", ["ambiguous_mapping", "contract_expired"])
async def test_any_other_denial_stops_the_chain(reason: str) -> None:
    first, second = Member(Denied(reason)), Member(KEY)
    assert await ChainResolver([first, second]).resolve(ID) == Denied(reason)
    assert second.calls == 0


async def test_an_error_stops_the_chain_and_fails_closed() -> None:
    first, second = Member(ResolverError("down")), Member(KEY)
    with pytest.raises(ResolverError):
        await ChainResolver([first, second]).resolve(ID)
    assert second.calls == 0


async def test_all_members_without_mapping_is_no_mapping() -> None:
    chain = ChainResolver([Member(Denied("no_mapping")), Member(Denied("no_mapping"))])
    assert await chain.resolve(ID) == Denied("no_mapping")


async def test_invalidate_and_close_reach_every_member_even_after_a_failure() -> None:
    members = [Member(KEY, invalidate_error=ResolverError("nope")), Member(KEY)]
    chain = ChainResolver(members)
    with pytest.raises(ResolverError):
        await chain.invalidate(ID)
    assert members[1].invalidated == 1
    await chain.aclose()
    assert all(m.closed for m in members)


def test_empty_chain_is_refused() -> None:
    with pytest.raises(ResolverError):
        ChainResolver([])


async def test_every_member_is_closed_even_when_one_close_fails() -> None:
    failing = Member(KEY, close_error=ResolverError("close failed"))
    after = Member(KEY)
    with pytest.raises(ResolverError, match="close failed"):
        await ChainResolver([failing, after]).aclose()
    assert failing.closed and after.closed
