# auth — model-invisible credential handling (D5).
#
# Pass A: RuntimeCredentials (this package). Pass B adds jwt_verify.py (local
# JWKS verification of the inbound JWT -> column_scope, mirroring
# clickhouse-api's app/auth_jwt.py) and app.py wires the HTTP entrypoint that
# constructs RuntimeCredentials from the Authorization/X-Session-Id headers.
from .credentials import RuntimeCredentials
from .jwt_verify import JWTVerificationError, verify_jwt

__all__ = ["JWTVerificationError", "RuntimeCredentials", "verify_jwt"]
