"""Category aliases shared by validation, prompt routing and catalogue search."""
CHAIN_ALIASES = frozenset({"chain", "chains", "neck chain", "neck chains"})


def normalize_chain_type(value: str) -> str:
    normalized = " ".join(value.strip().lower().split())
    return "chain" if normalized in CHAIN_ALIASES else normalized
