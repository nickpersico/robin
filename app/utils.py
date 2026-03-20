import secrets


def generate_id(prefix: str) -> str:
    """Generate a prefixed random ID like 'ro_a3b9c2d1e4f5'."""
    return f"{prefix}_{secrets.token_hex(6)}"
