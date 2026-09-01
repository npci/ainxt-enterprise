# SPDX-License-Identifier: Apache-2.0
# ============================================================
# PASSWORD HASHING CONTEXT
# Global passlib context for consistent password hashing/verification
# ============================================================

from passlib.context import CryptContext

# Global password context - used for hashing and verifying passwords
# Using bcrypt as the primary scheme with auto-deprecation of older schemes
pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")