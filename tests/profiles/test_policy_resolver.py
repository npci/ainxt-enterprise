# SPDX-License-Identifier: MIT
# ============================================================
# PolicyResolver / DomainProfile — pure-logic unit tests (Wave 1)
# ============================================================
#
# These import profiles.* directly — no gateway import required (gateway.py
# cannot be imported in a bare test env). They assert that the default profile
# reproduces today's runtime constants, and a CONSTANT-PARITY GUARD reads
# gateway.py as text to prove the default has not drifted from the real
# gateway constant (the same drift-guard technique as
# tests/agents/test_gateway_passthrough_logic.py).
# ============================================================

import os
import re

from profiles.resolver import ENTERPRISE_DEFAULT, EffectivePolicy, PolicyResolver
from profiles.schema import ContextPolicy, DomainProfile


GATEWAY_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "gateway.py",
)


# ── resolver behavior ───────────────────────────────────────────────────────

def test_default_resolve_reproduces_today():
    eff = PolicyResolver().resolve()
    assert isinstance(eff, EffectivePolicy)
    assert eff.usable_fraction == 0.75
    assert eff.history_retrieval_enabled is False
    assert eff.durable_memory_max_tokens == 800
    assert eff.profile_id == "enterprise_default"


def test_resolve_accepts_user_ctx_without_effect_in_wave1():
    # user_ctx is accepted (stable call signature) but does not change the
    # result in Wave 1 (org/role/user merge is deferred).
    a = PolicyResolver().resolve()
    b = PolicyResolver().resolve(user_ctx={"user_id": "u1", "org_id": "o1"})
    assert a == b


def test_explicit_profile_round_trips():
    prof = DomainProfile(profile_id="coding", context=ContextPolicy(usable_fraction=0.80))
    eff = PolicyResolver().resolve(profile=prof)
    assert eff.profile_id == "coding"
    assert eff.usable_fraction == 0.80


def test_enterprise_default_is_frozen_value_object():
    # frozen dataclass — attribute assignment must raise.
    import dataclasses
    assert dataclasses.is_dataclass(ENTERPRISE_DEFAULT)
    try:
        ENTERPRISE_DEFAULT.profile_id = "x"  # type: ignore[misc]
        raised = False
    except dataclasses.FrozenInstanceError:
        raised = True
    assert raised, "DomainProfile must be frozen"


# ── constant-parity drift guard (reads gateway.py as text) ──────────────────

def test_default_matches_gateway_usable_fraction_constant():
    """The default profile's usable_fraction must equal gateway's real constant.

    Proves ENTERPRISE_DEFAULT stays in sync with gateway.py:279 even though
    gateway.py cannot be imported. If someone changes the gateway default,
    this fails loudly and the profile default must be updated to match.
    """
    with open(GATEWAY_PATH, "r", encoding="utf-8") as f:
        src = f.read()
    m = re.search(r'CHAT_CONTEXT_USABLE_FRACTION"\s*,\s*"([0-9.]+)"', src)
    assert m, "could not find CHAT_CONTEXT_USABLE_FRACTION default in gateway.py"
    gateway_default = float(m.group(1))
    assert gateway_default == ENTERPRISE_DEFAULT.context.usable_fraction, (
        "ENTERPRISE_DEFAULT.context.usable_fraction drifted from "
        "gateway._CONTEXT_USABLE_FRACTION — update profiles/schema.py ContextPolicy"
    )
