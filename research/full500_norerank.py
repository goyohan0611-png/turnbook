"""The full500 packets with the learned re-ranker switched off (plain rank fusion of the word and
meaning searches), so no weight fitted on LongMemEval or LoCoMo touches the ranking: the held-out
counterpart of the headline 91.8%. Everything else is the shipped search.

    gh workflow run cache-merge --ref exp/gemma \
        -f runs="40842757612 40842769497 40842781053 40842793168 40842805327" \
        -f research="full500_norerank.py"
    # then, locally: product_answer_eval.py answer --out runs/graph-v9.14-full500-mpnet8-norerank
"""
from pathlib import Path
import runpy
import sys

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "src"))
from turnbook import mcp_server  # noqa: E402

mcp_server.rerank = lambda query, meaning, words, *, limit, weights=None: mcp_server.fuse(
    meaning, words, limit=limit)
sys.argv = [str(HERE / "full500_mpnet.py"), "norerank", *sys.argv[1:]]
runpy.run_path(sys.argv[0], run_name="__main__")
