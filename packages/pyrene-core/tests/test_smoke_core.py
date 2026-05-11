from uuid import uuid4

import pytest
from pydantic import ValidationError

from pyrene_core import Confidence, OrderBySpec, StrictBaseModel, UserContext


def test_version() -> None:
    import pyrene_core

    assert pyrene_core.__version__ == "0.1.0"


def test_confidence_values() -> None:
    assert Confidence.high == "high"
    assert {c.value for c in Confidence} == {"high", "medium", "low"}


def test_strict_base_model_forbids_extra() -> None:
    class Sample(StrictBaseModel):
        x: int

    Sample(x=1)

    with pytest.raises(ValidationError):
        Sample(x=1, y=2)  # type: ignore[call-arg]


def test_strict_base_model_is_frozen() -> None:
    class Sample(StrictBaseModel):
        x: int

    obj = Sample(x=1)
    with pytest.raises(ValidationError):
        obj.x = 2


def test_order_by_spec_default_asc() -> None:
    spec = OrderBySpec(column="created_at")
    assert spec.direction == "asc"


def test_order_by_spec_rejects_invalid_direction() -> None:
    with pytest.raises(ValidationError):
        OrderBySpec(column="x", direction="random")  # type: ignore[arg-type]


def test_user_context_minimal() -> None:
    ctx = UserContext(user_id=uuid4(), team_id=uuid4(), roles=("analyst",))
    assert ctx.roles == ("analyst",)


def test_user_context_rejects_extra_field() -> None:
    with pytest.raises(ValidationError):
        UserContext(  # type: ignore[call-arg]
            user_id=uuid4(),
            team_id=uuid4(),
            roles=(),
            email="x@y.z",
        )
