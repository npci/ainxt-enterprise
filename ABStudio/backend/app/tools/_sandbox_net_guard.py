# SPDX-License-Identifier: Apache-2.0
# Canonical sandbox network policy — shared by document_tools.py's
# read_document (SSRF guard on its `url` input) and platform_tools.py's
# code_executor (in-process egress guard on `socket.socket.connect`).
#
# This module is used in two different ways:
#   1. Normally imported by the PARENT process (which has app.* on its
#      PYTHONPATH), purely so `inspect.getsource()` can read its own source
#      text back out as a plain string.
#   2. That source text is then pasted verbatim into the embedded sandbox
#      source strings (_READ_DOCUMENT_CODE / _CODE_EXECUTOR_CODE) which get
#      base64-encoded and exec'd in a subprocess with NO app.* access — so
#      this module must be 100% self-contained, stdlib-only code with no
#      imports from anywhere else in this codebase.
#
# Keeping the IP-range lists and the DNS-resolution mechanics in exactly one
# file makes drift between the two sandbox copies structurally impossible:
# edit this file once, and the next process restart (which re-reads this
# module's source at import time) picks up the identical code in both
# places. See ABStudio_Security_Review.html F-01/F-02 remediation notes and
# the follow-up code review that flagged the original hand copy-pasted
# lists as already out of sync.
#
# IMPORTANT — the two callers use OPPOSITE polarity, so this module exposes
# two distinct network sets rather than one shared "private nets" list:
#   * read_document fetches ARBITRARY external URLs, so its guard is a
#     DENYLIST: block private/loopback/link-local/reserved destinations
#     (SSRF prevention), allow everything else (the public internet is the
#     whole point of the `url` input).
#   * code_executor runs arbitrary LLM-generated code with no legitimate
#     need for network access at all, so its guard is an ALLOWLIST: permit
#     ONLY a curated set of intranet ranges, and block everything else —
#     including the public internet AND link-local/metadata addresses.
# Collapsing these into one list would be a correctness bug: e.g. adding
# 169.254.0.0/16 to a single shared "private" list and using it as
# code_executor's ALLOWlist would flip the cloud-metadata endpoint from
# blocked to reachable. Keeping them as two clearly-named sets below makes
# that mistake structurally harder to make by accident.

import ipaddress
import os
import socket
import threading

# Curated intranet ranges — code_executor may reach ONLY these (allowlist).
# Loopback + RFC1918 + CGNAT + IPv6 ULA. Deliberately does NOT include
# link-local (169.254.0.0/16 / fe80::/10 — covers the cloud metadata
# endpoint), 0.0.0.0/8, the IETF protocol-assignment block, or the
# benchmark-testing range: none of those are legitimate intranet
# destinations for LLM-generated code, so they must stay excluded from this
# allowlist regardless of how the denylist below evolves.
_INTRANET_NETS = [
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("100.64.0.0/10"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
]

# Ranges that must NEVER be reachable regardless of context — link-local
# (covers the cloud metadata endpoint at 169.254.169.254), "this network",
# IETF protocol assignments, and the benchmark-testing block. These are
# never "intranet" and never a safe SSRF target either.
_NEVER_CONNECT_NETS = [
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),   # link-local + cloud metadata
    ipaddress.ip_network("192.0.0.0/24"),     # IETF protocol assignments
    ipaddress.ip_network("198.18.0.0/15"),    # benchmark testing
    ipaddress.ip_network("fe80::/10"),
]

# The full private/loopback/link-local/reserved set — used by
# read_document's SSRF DENYLIST (block if the resolved IP is in here; allow
# everything else, since fetching public URLs is the tool's whole purpose).
_PRIVATE_OR_RESERVED_NETS = _INTRANET_NETS + _NEVER_CONNECT_NETS

_SANDBOX_NET_TIMEOUT = float(os.environ.get("SANDBOX_NET_TIMEOUT", "15"))


def _resolve_host_with_timeout(host):
    # Resolve host via a daemon thread bounded by _SANDBOX_NET_TIMEOUT so a
    # black-holed / slow DNS server can't hang the caller indefinitely.
    # Returns the resolved IP string, or raises on failure/timeout. Shared
    # by both the URL-fetch guard (document_tools) and the raw-socket-
    # connect guard (platform_tools) so the exact same bounded-DNS
    # behaviour applies at both call sites.
    resolved = {}

    def _resolve():
        try:
            resolved["ip"] = socket.gethostbyname(host)
        except Exception as exc:  # noqa: BLE001
            resolved["err"] = exc

    t = threading.Thread(target=_resolve, daemon=True)
    t.start()
    t.join(_SANDBOX_NET_TIMEOUT)
    if t.is_alive():
        raise OSError("DNS lookup for %r timed out after %ss" % (host, _SANDBOX_NET_TIMEOUT))
    if "err" in resolved:
        raise resolved["err"]
    return resolved["ip"]


def _is_private_or_reserved_ip(ip_str):
    # For read_document's DENYLIST: True → block (SSRF prevention).
    ip = ipaddress.ip_address(ip_str)
    return any(ip in net for net in _PRIVATE_OR_RESERVED_NETS)


def _is_intranet_ip(ip_str):
    # For code_executor's ALLOWLIST: True → permit. Deliberately does NOT
    # consult _NEVER_CONNECT_NETS — those ranges must always evaluate to
    # "not intranet" here, which is exactly the current (correct) behaviour.
    ip = ipaddress.ip_address(ip_str)
    return any(ip in net for net in _INTRANET_NETS)
