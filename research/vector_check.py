"""Are the bench's cached vectors the model's vectors? For a few questions, every user piece's cached
vector against one embedded afresh. A packet built in CI held 1 of 4 answer turns where the same
code on a PC held all 4.

    python vector_check.py gpt4_d84a3211 c4a1ceb8
"""
import os
import sys
import tempfile

import episode_eval  # noqa: F401  (first: isolates TURNBOOK_HOME, sets up the paths)

from evidence import CACHE, DATA, fill  # noqa: E402
from longmemeval_m_fetch import stream_instances  # noqa: E402

import numpy as np  # noqa: E402

from turnbook.conversation_memory import ConversationMemoryStore  # noqa: E402
from turnbook.packing import unpack  # noqa: E402
from turnbook.local_embedder import LocalEmbedder  # noqa: E402
from turnbook.warmup import turn_pieces  # noqa: E402
from pathlib import Path  # noqa: E402

cached = LocalEmbedder(CACHE / "embed-cache.json")
fresh = LocalEmbedder(Path(tempfile.mkdtemp()) / "fresh.json")
for row in stream_instances(DATA):
    if row["question_id"] not in sys.argv[1:]:
        continue
    store = Path(tempfile.mkdtemp()) / "s.sqlite"
    fill(store, row)
    with ConversationMemoryStore(store) as s:
        texts = [p for (plain, packed) in s.db.execute(
                     "SELECT content_redacted, content_z FROM conversation_turns "
                     "WHERE role='user'")
                 for _, p in turn_pieces(unpack(plain, packed))]
    known = [t for t in texts if cached._key(t.strip()[:2000]) in cached.cache]
    a = np.asarray(cached.embed(known))
    b = np.asarray(fresh.embed(known))
    cos = (a * b).sum(1) / (np.linalg.norm(a, axis=1) * np.linalg.norm(b, axis=1))
    print(row["question_id"], "pieces", len(texts), "cached", len(known),
          "cosine min", round(float(cos.min()), 4), "median", round(float(np.median(cos)), 4),
          "below 0.99", int((cos < 0.99).sum()), flush=True)
print(os.environ.get("GITHUB_SHA", "local"))
