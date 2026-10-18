"""64-bit SimHash (Charikar, 2002) for duplicate and near-duplicate pages."""
import hashlib
import re

_WORD = re.compile(r"[a-z0-9]+")


def _features(text: str, k: int = 3) -> list[str]:
    words = _WORD.findall(text.lower())
    if len(words) < k:
        return words
    return [" ".join(words[i:i + k]) for i in range(len(words) - k + 1)]


def simhash(text: str, bits: int = 64) -> int:
    v = [0] * bits
    for feat in _features(text):
        h = int.from_bytes(hashlib.md5(feat.encode()).digest()[:8], "big")
        for i in range(bits):
            v[i] += 1 if (h >> i) & 1 else -1
    out = 0
    for i in range(bits):
        if v[i] > 0:
            out |= 1 << i
    return out


def distance(a: int, b: int) -> int:
    return bin(a ^ b).count("1")


def find_duplicates(pages: dict[str, int], threshold: int = 3) -> list[tuple[str, str, int]]:
    """Return (url_a, url_b, hamming distance) for pairs within threshold."""
    urls = sorted(pages)
    pairs = []
    for i, a in enumerate(urls):
        for b in urls[i + 1:]:
            d = distance(pages[a], pages[b])
            if d <= threshold:
                pairs.append((a, b, d))
    return pairs
