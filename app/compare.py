"""Compares an audit with the previous finished audit of the same URL."""
from __future__ import annotations


def compare(old: dict, new: dict) -> dict:
    before = {i["id"]: i for i in old.get("issues", [])}
    after = {i["id"]: i for i in new.get("issues", [])}

    def brief(i: dict) -> dict:
        return {"id": i["id"], "title": i["title"], "level": i["level"], "pages": i["pages_affected"]}

    changed = []
    for k in before.keys() & after.keys():
        b, a = before[k]["pages_affected"], after[k]["pages_affected"]
        if a != b:
            changed.append({**brief(after[k]), "before": b, "after": a, "delta": a - b})
    changed.sort(key=lambda c: (c["delta"], c["id"]))

    return {
        "health_before": old.get("health_score"),
        "health_after": new.get("health_score"),
        "health_delta": (new.get("health_score") or 0) - (old.get("health_score") or 0),
        "pages_before": old.get("pages_crawled"),
        "pages_after": new.get("pages_crawled"),
        "fixed": [brief(before[k]) for k in before if k not in after],
        "new": [brief(after[k]) for k in after if k not in before],
        "changed": changed,
    }
