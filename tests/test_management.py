import pytest

from app.telegram_gateway.management import simulate_post_activity


@pytest.mark.asyncio
async def test_activity_simulation_is_local_only():
    result = await simulate_post_activity(
        post_key="test-post", requested_views=1200, requested_reactions=35
    )

    assert result["mode"] == "simulation"
    assert result["simulated_views"] == 1200
    assert result["simulated_reactions"] == 35
    assert result["telegram_modified"] is False


@pytest.mark.asyncio
async def test_activity_simulation_clamps_values():
    result = await simulate_post_activity(
        post_key="test-post", requested_views=2_000_000, requested_reactions=-10
    )

    assert result["simulated_views"] == 1_000_000
    assert result["simulated_reactions"] == 0


@pytest.mark.asyncio
async def test_activity_simulation_requires_post_key():
    with pytest.raises(ValueError, match="идентификатор"):
        await simulate_post_activity(
            post_key=" ", requested_views=10, requested_reactions=1
        )
