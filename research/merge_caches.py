"""Join embedding caches restored from several bench runs (cache-merge workflow) into bench/.cache:
every vector of the same model that bench/.cache lacks is added.

    python research/merge_caches.py DIR [DIR ...]
"""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from turnbook.vector_cache import VectorCache  # noqa: E402

CACHE = Path(__file__).resolve().parent.parent / "bench/.cache"
for source in map(Path, sys.argv[1:]):
    for folder in sorted(source.glob("embed-*.vec")):
        theirs, mine = VectorCache(folder), VectorCache(CACHE / folder.name)
        added = 0
        for key in theirs.keys:
            if key not in mine:
                mine.put(key, theirs.get(key))
                added += 1
        mine.flush()
        print(f"{folder.name}: +{added} from {source.name}, {len(mine)} in all", flush=True)
