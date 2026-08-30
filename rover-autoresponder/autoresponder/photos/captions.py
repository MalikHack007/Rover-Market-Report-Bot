"""Caption pool — pre-written lines, NO LLM.

One caption per line in captions.txt (blank lines and `# comments` ignored). A `{pet}`
placeholder is substituted with the dog's name. `pick()` avoids the immediately-previous
caption for a thread (a light anti-repeat), so an owner doesn't get the same line twice
running.
"""
import random

from . import config


def load_pool(path=None):
    path = path or config.CAPTIONS_PATH
    try:
        with open(path, encoding="utf-8") as fh:
            lines = [ln.strip() for ln in fh]
    except FileNotFoundError:
        return []
    return [ln for ln in lines if ln and not ln.startswith("#")]


def pick(pet_name, pool=None, avoid_index=None):
    """Return (caption_text, index). Substitutes {pet}; avoids `avoid_index` when possible.

    The returned index is stored per thread (meta `caption_last:<thread>`) and passed back as
    `avoid_index` next time, so consecutive updates to the same owner differ.
    """
    pool = pool if pool is not None else load_pool()
    if not pool:                                    # empty/missing pool → a safe default
        return (f"{pet_name or 'Your pup'} is having a great time!", -1)
    candidates = list(range(len(pool)))
    if avoid_index is not None and len(pool) > 1 and 0 <= avoid_index < len(pool):
        candidates.remove(avoid_index)
    idx = random.choice(candidates)
    text = pool[idx].replace("{pet}", pet_name or "your pup")
    return (text, idx)
