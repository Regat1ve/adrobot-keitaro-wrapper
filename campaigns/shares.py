"""Деление трафика между офферами потока: целые проценты без потерь."""


def split_shares(n: int, total: int = 100) -> list[int]:
    """Делит total поровну на n частей. Остаток по единице уходит последним: 3 -> [33, 33, 34], как в Keitaro."""
    if n <= 0:
        return []
    base, rest = divmod(total, n)
    return [base] * (n - rest) + [base + 1] * rest
