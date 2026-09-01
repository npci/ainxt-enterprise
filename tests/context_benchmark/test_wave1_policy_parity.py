# SPDX-License-Identifier: Apache-2.0
# ============================================================
# Wave 1 policy parity — behavioral equivalence check
# ============================================================
#
# The only Wave-1 value that downstream code will eventually READ from the new
# EffectivePolicy (instead of the inline gateway constant) is usable_fraction.
# This test proves the resolver reproduces gateway's real runtime value, so the
# eventual swap is behavior-neutral. It lives alongside the context benchmark
# because usable_fraction is the context-assembly budget knob (gateway.py:279).
#
# Pure/offline/deterministic — no gateway import (read as text), no LLM, no I/O.
# ============================================================

import os
import re

from profiles.resolver import ENTERPRISE_DEFAULT, PolicyResolver


_GATEWAY = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "gateway.py",
)


def _gateway_usable_fraction_default() -> float:
    src = open(_GATEWAY, encoding="utf-8").read()
    m = re.search(r'CHAT_CONTEXT_USABLE_FRACTION"\s*,\s*"([0-9.]+)"', src)
    assert m, "gateway CHAT_CONTEXT_USABLE_FRACTION default not found"
    return float(m.group(1))


def test_resolver_usable_fraction_equals_gateway_default():
    resolved = PolicyResolver().resolve().usable_fraction
    assert resolved == _gateway_usable_fraction_default(), (
        "EffectivePolicy.usable_fraction must equal gateway's runtime default so "
        "later waves can read the policy instead of the inline constant without "
        "changing context-assembly behavior"
    )


def test_default_profile_history_retrieval_matches_reality():
    # gateway has no history-retrieval feature yet, so the default profile must
    # keep it False (reproduces today).
    assert ENTERPRISE_DEFAULT.context.history_retrieval_enabled is False
