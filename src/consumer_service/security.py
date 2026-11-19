import hmac

from pydantic import SecretStr


def is_authorized(authorization: str | None, token: SecretStr) -> bool:
    """Cek header "Authorization: Bearer <token>" dengan perbandingan waktu-konstan."""
    if not authorization:
        return False
    scheme, _, supplied = authorization.partition(" ")
    if scheme.lower() != "bearer" or not supplied:
        return False
    expected = token.get_secret_value()
    return hmac.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8"))
