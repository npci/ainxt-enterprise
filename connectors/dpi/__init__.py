# SPDX-License-Identifier: MIT
"""
AiNxt DPI (Digital Public Infrastructure) agent connectors.

Lets AI agents act on India Stack rails — Account Aggregator (consent-based
financial data), DigiLocker (verified documents), and (later) UPI / BBPS / ONDC.

The defining property vs. ordinary OAuth connectors: DPI uses a signed CONSENT
ARTIFACT (Account Aggregator / DEPA model), not a bearer token. A user grants
consent for a specific PURPOSE and DATA RANGE, time-bound and revocable.

This whole package is self-contained so it can be extracted into a standalone
open-source repo (`ainxt-dpi-agent`) — framework + synthetic sandbox only,
never real endpoints or credentials. Real production access is gated by each
org's own DPI licensing (RBI/UIDAI), env-injected.

Open by default in SANDBOX mode (env `DPI_SANDBOX=true`): runs fully offline
against synthetic fixtures so anyone can build/test DPI agents without licensing.
"""

DPI_CONNECTOR_PREFIX = "dpi_"
