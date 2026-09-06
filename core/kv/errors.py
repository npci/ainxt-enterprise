# SPDX-License-Identifier: MIT
# ============================================================
# KV backend exceptions
#
# Unified exception hierarchy so call sites can catch one type
# regardless of which backend is in use. Backend-specific
# exceptions are wrapped at the KVClient layer.
# ============================================================


class KVError(Exception):
    """Base class for all KV-layer errors."""


class KVTransient(KVError):
    """
    Transient failure — connection refused, timeout, temporary outage.
    Generally retryable. The client may also have retried internally;
    by the time this is raised, retries are exhausted.
    """


class KVPermanent(KVError):
    """
    Permanent failure — authentication, config, protocol, or programmer error.
    Not retryable. Log and surface to the caller.
    """
