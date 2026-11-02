"""Your memory as a 3D graph, in the browser: every conversation turn and memory is a node, joined
by the associations Turnbook already keeps (shared cues, neighbours in time, explicit links).

    turnbook-view               # writes ~/.turnbook/brain.html and opens it
    turnbook-view --no-text     # no conversation text in the file: safe to screenshot or share
    turnbook-view --limit 2000  # only the most recent 2,000 nodes

The page is a local file. It loads the 3D library from a CDN; your memory itself never leaves the PC.
"""
from __future__ import annotations

from pathlib import Path
import argparse
import collections
import json
import math
import sqlite3
import webbrowser

from turnbook.packing import text_columns, unpack

LINKS_PER_NODE = 3          # strongest shared-cue links kept per node: all 134k drown the picture
REGIONS = 12                # coloured clusters named in the legend
PALETTE = ["#ff6b6b", "#ffd93d", "#6bcB77", "#4d96ff", "#c77dff", "#ff9f1c", "#2ec4b6",
           "#f15bb5", "#9ef01a", "#00bbf9", "#fee440", "#e07a5f"]


def read_graph(db_path, limit, with_text):
    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        return _graph(db, limit, with_text)
    finally:
        db.close()


def read_timeline(db_path, limit, with_text):
    """Read recent captured turns even when the derived association index is still catching up."""
    if not with_text:
        return []
    db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        if not db.execute("SELECT 1 FROM sqlite_master WHERE name='conversation_turns'").fetchone():
            return []
        packed = "content_z" if "content_z" in text_columns(db) else "NULL"
        rows = db.execute(f"""SELECT client,client_session_id,scope,happened_at,role,
                                     content_redacted,{packed}
                              FROM conversation_turns WHERE role IN ('user','assistant')
                              ORDER BY happened_at DESC,revision DESC LIMIT ?""", (limit,))
        return [{"app": app, "session": session, "scope": scope, "when": when,
                 "role": role, "text": unpack(text, compressed)[:1600],
                 "work": unpack(text, compressed).lstrip().startswith("Did:")}
                for app, session, scope, when, role, text, compressed in rows]
    finally:
        db.close()


def _graph(db, limit, with_text):
    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if "associative_engrams" not in tables:
        return [], [], {}
    # a store only has the tables of what it has held so far
    source = lambda table, columns: (table if table in tables  # noqa: E731
                                     else f"(SELECT {columns} WHERE 0)")
    packed = ("t.content_z" if "conversation_turns" in tables
              and "content_z" in text_columns(db) else "NULL")
    nodes = db.execute(
        f"""SELECT e.engram_id, e.source_kind, e.scope, e.happened_at,
                  COALESCE(t.content_redacted, m.title || ': ' || m.content, ''),
                  COALESCE(t.role, ''), {packed}
           FROM associative_engrams e
           LEFT JOIN {source("conversation_turns", "'' turn_id, '' content_redacted, '' role")} t
             ON e.source_kind='CONVERSATION' AND t.turn_id=e.source_id
           LEFT JOIN {source("local_brain_memories", "'' memory_id, '' title, '' content")} m
             ON e.source_kind='LOCAL_MEMORY' AND m.memory_id=e.source_id
           WHERE e.source_kind != 'DEVELOPMENT_EVENT'      -- notes of a feature gone in 0.8
           ORDER BY e.happened_at DESC LIMIT ?""", (limit,)).fetchall()
    keep = {row[0] for row in nodes}
    edges = db.execute(
        """SELECT source_engram_id, target_engram_id, relation, weight FROM (
             SELECT *, ROW_NUMBER() OVER (PARTITION BY source_engram_id
                                          ORDER BY weight DESC, evidence_count DESC) AS n
             FROM associative_edges WHERE relation='SHARED_CUE')
           WHERE n<=?
           UNION ALL
           SELECT source_engram_id, target_engram_id, relation, weight
           FROM associative_edges WHERE relation!='SHARED_CUE'""", (LINKS_PER_NODE,)).fetchall()
    terms = collections.defaultdict(list)
    for term, engram in db.execute("SELECT term, engram_id FROM associative_terms"):
        if engram in keep:
            terms[engram].append(term)
    links, seen = [], set()
    for source, target, relation, weight in edges:
        pair = tuple(sorted((source, target)))
        if source in keep and target in keep and source != target and pair not in seen:
            seen.add(pair)
            links.append({"source": source, "target": target, "kind": relation,
                          "weight": round(weight, 3)})
    out = []
    for engram, kind, scope, when, text, role, compressed in nodes:
        if compressed is not None:
            text = unpack(text, compressed)
        node = {"id": engram, "kind": kind, "scope": scope, "when": (when or "")[:16],
                "role": role}
        if with_text:
            node["text"] = " ".join((text or "").split())[:160]
        out.append(node)
    return out, links, terms


def regions(nodes, links, terms):
    """Label propagation: each node takes the region most of its neighbours are in. Regions are
    named by the cue words most particular to them."""
    neighbours = collections.defaultdict(list)
    for link in links:
        neighbours[link["source"]].append((link["target"], link["weight"]))
        neighbours[link["target"]].append((link["source"], link["weight"]))
    label = {node["id"]: node["id"] for node in nodes}
    for _ in range(12):
        changed = 0
        for node in nodes:
            votes = collections.Counter()
            for other, weight in neighbours[node["id"]]:
                votes[label[other]] += weight
            if votes:
                best = max(votes.items(), key=lambda item: (item[1], item[0]))[0]
                if best != label[node["id"]]:
                    label[node["id"]], changed = best, changed + 1
        if not changed:
            break
    top = [region for region, _ in collections.Counter(label.values()).most_common(REGIONS)]
    for _ in range(6):        # small regions join the large region they are most linked to
        for node in nodes:
            if label[node["id"]] in top:
                continue
            votes = collections.Counter()
            for other, weight in neighbours[node["id"]]:
                if label[other] in top:
                    votes[label[other]] += weight
            if votes:
                label[node["id"]] = votes.most_common(1)[0][0]
    sizes = collections.Counter(label.values())
    everywhere = collections.Counter(term for node in nodes for term in set(terms[node["id"]]))
    names = {}
    for region in top:
        members = [node["id"] for node in nodes if label[node["id"]] == region]
        local = collections.Counter(term for m in members for term in set(terms[m]))
        # words only: ids, hashes and session numbers ("676b428b") are frequent cues, not topics
        score = {term: n * math.log(len(nodes) / everywhere[term])
                 for term, n in local.items()
                 if len(term) > 1 and n > 1 and not any(c.isdigit() for c in term)}
        names[region] = [term for term, _ in sorted(score.items(), key=lambda i: -i[1])[:3]]
    for node in nodes:
        region = label[node["id"]]
        node["region"] = top.index(region) if region in top else -1
        node["degree"] = len(neighbours[node["id"]])
    return [{"color": PALETTE[i], "size": sizes[region], "words": names[region]}
            for i, region in enumerate(top)]


PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Turnbook brain</title>
<style>
:root { --bg:#05060a; --fg:#e8ecf4; --dim:#8a93a6; --panel:rgba(14,17,26,.82); }
html,body { margin:0; height:100%; background:var(--bg); color:var(--fg);
  font:14px/1.45 system-ui,-apple-system,"Segoe UI","Malgun Gothic",sans-serif; overflow:hidden; }
#graph { position:fixed; inset:0; }
.panel { position:fixed; background:var(--panel); border:1px solid #232a3a; border-radius:10px;
  padding:12px 14px; backdrop-filter:blur(6px); max-width:min(360px, calc(100vw - 32px)); }
#head { top:16px; left:16px; } #head h1 { margin:0 0 4px; font-size:18px; letter-spacing:.3px; }
#head p { margin:0; color:var(--dim); }
#legend { bottom:16px; left:16px; max-height:42vh; overflow:auto; }
#legend div { display:flex; gap:8px; align-items:center; margin:3px 0; }
#legend i { width:10px; height:10px; border-radius:50%; flex:none; }
#info { top:16px; right:16px; display:none; }
#info b { display:block; margin-bottom:4px; } #info small { color:var(--dim); }
button { position:fixed; bottom:16px; right:16px; background:var(--panel); color:var(--fg);
  border:1px solid #232a3a; border-radius:8px; padding:8px 12px; cursor:pointer; }
[hidden] { display:none !important; }
button:hover { border-color:#4d96ff; } :focus-visible { outline:2px solid #4d96ff; outline-offset:3px; }
#head button { position:static; margin-top:10px; }
#timeline { top:130px; bottom:68px; left:16px; width:620px; max-width:calc(100vw - 64px);
  display:flex; flex-direction:column; padding:16px; background:#0e111af5; }
#timeline h2 { margin:0; font-size:18px; } #timeline header { display:flex; justify-content:space-between; align-items:center; gap:20px; }
#timeline button { position:static; }
#timeline .filters { display:flex; gap:12px; flex-wrap:wrap; margin:14px 0 8px; }
#timeline label { display:flex; flex-direction:column; gap:5px; color:var(--dim); font-size:12px; }
#timeline .search { flex:1; min-width:160px; }
#timeline input,#timeline select { font:inherit; color:var(--fg); background:#05060a; border:1px solid #343d50; border-radius:5px; padding:8px; }
#timeline .work-filter { flex-direction:row; align-items:center; align-self:flex-end; min-height:36px; }
#timeline-count { color:var(--dim); font-size:12px; margin:4px 0 12px; }
#timeline-list { overflow:auto; flex:1; min-height:0; padding-right:8px; }
#timeline details { border-top:1px solid #30384a; padding:12px 0; }
#timeline summary { cursor:pointer; font-weight:600; color:#c9d7ee; }
#timeline article { border-left:2px solid #30384a; padding:0 0 0 14px; margin:14px 0 20px 4px; }
#timeline article.work { border-color:#2ec4b6; }
#timeline small { color:var(--dim); } #timeline p { white-space:pre-wrap; overflow-wrap:anywhere; margin:5px 0 0; }
.timeline-open #info { visibility:hidden; }
@media(max-width:520px) { #timeline { top:142px; } #timeline .filters { gap:8px; } }
</style></head><body>
<div id="graph"></div>
<div class="panel" id="head"><h1>Turnbook</h1><p id="stats"></p><button id="open-timeline" aria-expanded="false" aria-controls="timeline">Sessions &amp; work</button></div>
<div class="panel" id="legend"></div>
<div class="panel" id="info"></div>
<section class="panel" id="timeline" aria-label="Session timeline" hidden>
<header><h2>Sessions &amp; work</h2><button id="close-timeline" aria-label="Close timeline">Close</button></header>
<div class="filters"><label class="search">Search<input id="timeline-query" type="search" placeholder="A decision, file or conversation"></label>
<label>Project<select id="timeline-scope"><option value="">All projects</option></select></label>
<label class="work-filter"><input id="timeline-work" type="checkbox">Work only</label></div>
<p id="timeline-count" aria-live="polite"></p><div id="timeline-list"></div>
</section>
<button id="spin">Pause rotation</button>
__LIBRARY__
<script>
const DATA = __DATA__;
const regions = DATA.regions, grey = "#4a5266";
const color = n => n.region >= 0 ? regions[n.region].color : grey;
document.getElementById("stats").textContent =
  `${DATA.nodes.length.toLocaleString()} memories · ${DATA.links.length.toLocaleString()} links · since ${DATA.since}`;
document.getElementById("legend").innerHTML = regions.map(r =>
  `<div><i style="background:${r.color}"></i><span>${r.words.length ? r.words.join(" · ") : "region"} <small style="color:var(--dim)">(${r.size})</small></span></div>`).join("");
const info = document.getElementById("info");
const esc = s => String(s).replace(/[&<>]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;"}[c]));
const timeline = document.getElementById("timeline"), openTimeline = document.getElementById("open-timeline");
const query = document.getElementById("timeline-query"), scope = document.getElementById("timeline-scope");
const workOnly = document.getElementById("timeline-work"), list = document.getElementById("timeline-list");
for (const name of [...new Set(DATA.timeline.map(t => t.scope))].sort()) {
  const option = document.createElement("option"); option.value = name; option.textContent = name; scope.append(option);
}
function renderTimeline(cap=100) {
  const q = query.value.trim().toLocaleLowerCase();
  const matched = DATA.timeline.filter(t => (!scope.value || t.scope === scope.value) &&
    (!workOnly.checked || t.work) && (!q || [t.text,t.app,t.scope].join(" ").toLocaleLowerCase().includes(q)));
  document.getElementById("timeline-count").textContent = DATA.textIncluded ?
    `${matched.length} matching turns in this snapshot · showing ${Math.min(cap,matched.length)} · up to 1,600 characters per turn` :
    "Text was left out of this page. Run turnbook-view without --no-text to see sessions.";
  list.replaceChildren(); const groups = new Map();
  for (const t of matched.slice(0,cap)) {
    const key = JSON.stringify([t.app,t.session]);
    if (!groups.has(key)) groups.set(key, []); groups.get(key).push(t);
  }
  for (const turns of groups.values()) {
    const group = document.createElement("details"), heading = document.createElement("summary"), t = turns[0];
    group.open = groups.size < 4;
    heading.textContent = `${t.app} · ${t.when.slice(0,10)} · ${t.scope} · session ${t.session.slice(-8)}`;
    group.append(heading);
    for (const turn of [...turns].reverse()) {
      const entry = document.createElement("article"), meta = document.createElement("small"), text = document.createElement("p");
      if (turn.work) entry.className = "work";
      meta.textContent = `${turn.when.slice(0,16).replace("T"," ")} · ${turn.work ? "Work record" : turn.role}`;
      text.textContent = turn.text; entry.append(meta,text); group.append(entry);
    }
    list.append(group);
  }
  if (!matched.length && DATA.textIncluded) { const p=document.createElement("p"); p.textContent="No matching turns in this snapshot."; list.append(p); }
  if (matched.length > cap) { const more=document.createElement("button"); more.textContent="Show 100 more"; more.onclick=()=>renderTimeline(cap+100); list.append(more); }
}
function showTimeline(show) {
  timeline.hidden = !show; openTimeline.setAttribute("aria-expanded", String(show));
  document.body.classList.toggle("timeline-open", show);
  if (show) { renderTimeline(); query.focus(); } else openTimeline.focus();
}
openTimeline.onclick=()=>showTimeline(timeline.hidden);
document.getElementById("close-timeline").onclick=()=>showTimeline(false);
document.addEventListener("keydown", e=>{if(e.key==="Escape" && !timeline.hidden) showTimeline(false);});
query.oninput=()=>renderTimeline(); scope.onchange=()=>renderTimeline(); workOnly.onchange=()=>renderTimeline();
function drawGraph() {
const reducedMotion = matchMedia("(prefers-reduced-motion: reduce)").matches;
const graph = ForceGraph3D()(document.getElementById("graph"))
  .backgroundColor("#05060a")
  .nodeColor(color)
  .nodeRelSize(2.2)
  .nodeVal(n => 0.6 + Math.sqrt(n.degree) / 2)
  .nodeOpacity(0.9)
  .nodeLabel(() => "")
  .linkColor(l => l.kind === "TEMPORAL_ADJACENT" ? "rgba(140,160,200,0.25)" : "rgba(255,255,255,0.12)")
  .linkWidth(0)
  .onNodeHover(n => {
    document.body.style.cursor = n ? "pointer" : null;
    if (!n) { info.style.display = "none"; return; }
    info.style.display = "block";
    info.innerHTML = `<b style="color:${color(n)}">${esc(n.kind === "CONVERSATION" ? (n.role || "turn") : n.kind.toLowerCase())}</b>` +
      (n.text ? esc(n.text) + "<br>" : "") + `<small>${esc(n.when)} · ${esc(n.scope)} · ${n.degree} links</small>`;
  })
  .onNodeClick(n => {
    const d = 120 / Math.hypot(n.x, n.y, n.z), k = 1 + d;
    graph.cameraPosition({x: n.x * k, y: n.y * k, z: n.z * k}, n, reducedMotion ? 0 : 1200);
  });
// each region gathers around its own point on a sphere: lobes instead of one mixed ball
const centers = regions.map((_, i) => {
  const y = 1 - 2 * (i + 0.5) / regions.length, r = Math.sqrt(1 - y * y), t = i * 2.39996;
  return {x: 380 * r * Math.cos(t), y: 380 * y, z: 380 * r * Math.sin(t)};
});
// start every node near its region's point, so the lobes show from the first frame instead of
// depending on how long the browser lets the layout run (a background tab barely runs it)
DATA.nodes.forEach(n => {
  const c = n.region >= 0 ? centers[n.region] : {x: 0, y: 0, z: 0}, s = 70;
  n.x = c.x + (Math.random() - 0.5) * s; n.y = c.y + (Math.random() - 0.5) * s;
  n.z = c.z + (Math.random() - 0.5) * s;
});
const lobes = alpha => graph.graphData().nodes.forEach(n => {
  const c = n.region >= 0 ? centers[n.region] : {x: 0, y: 0, z: 0}, k = n.region >= 0 ? 0.35 : 0.05;
  n.vx += (c.x - n.x) * k * alpha; n.vy += (c.y - n.y) * k * alpha; n.vz += (c.z - n.z) * k * alpha;
});
graph.d3Force("lobes", lobes);
graph.d3Force("charge").strength(-12);
graph.d3Force("link").strength(l => l.kind === "SHARED_CUE" ? 0.05 : 0.01);
// data last, once every force is in place: reheating a layout that did not exist yet threw
// "reading 'tick'" on every frame and left the page black
graph.graphData({nodes: DATA.nodes, links: DATA.links});
let angle = 0, spinning = !reducedMotion;
document.getElementById("spin").textContent = spinning ? "Pause rotation" : "Rotate";
setInterval(() => { if (!spinning) return; angle += 0.0025;
  const r = 900; graph.cameraPosition({x: r * Math.sin(angle), z: r * Math.cos(angle)}); }, 30);
document.getElementById("spin").onclick = e => {
  spinning = !spinning; e.target.textContent = spinning ? "Pause rotation" : "Rotate"; };
}
if (typeof ForceGraph3D !== "undefined") drawGraph();
else {
  document.getElementById("graph").innerHTML = '<p style="padding:155px 24px;text-align:center;color:#8a93a6">The 3D library is unavailable. Sessions &amp; work is still available.</p>';
  document.getElementById("spin").hidden = true;
}
</script></body></html>
"""


LIBRARY = "https://unpkg.com/3d-force-graph@1.73.3/dist/3d-force-graph.min.js"
# The page holds the whole memory, so the library must be exactly the one checked (same bytes
# from unpkg and jsDelivr): a CDN serving anything else could read every memory in the page.
LIBRARY_SHA256 = "19b3be27040fc894e56d684d53c5f62526c25e5d27f37e8fe91fafff605e4ef4"
LIBRARY_SRI = "sha384-SIcVySj+Cd1g+cwoLNCdr/osXU15HLXCxfaSzFNkZICYeKS7I2YxhyggCijT8JHA"


def _library_tag(inline: bool) -> str:
    """The 3D library inside the page, fetched once by Python and kept in ~/.turnbook/vendor.
    Loaded from the CDN by the browser instead, a page opened from disk stayed black on a PC
    whose browser would not fetch it. Either way only the pinned bytes run."""
    fallback = (f'<script src="{LIBRARY}" integrity="{LIBRARY_SRI}" '
                'crossorigin="anonymous"></script>')
    if not inline:
        return fallback
    import hashlib

    from turnbook.paths import turnbook_home
    cached = turnbook_home() / "vendor" / LIBRARY.rsplit("/", 1)[-1].replace(
        ".min.js", "-1.73.3.min.js")
    if cached.exists() and hashlib.sha256(cached.read_bytes()).hexdigest() != LIBRARY_SHA256:
        cached.unlink()                      # kept by a version that did not check it
    if not cached.exists():
        import urllib.request
        try:
            code = urllib.request.urlopen(LIBRARY, timeout=60).read()
        except OSError:
            return fallback                  # offline now: the browser may still fetch it
        if hashlib.sha256(code).hexdigest() != LIBRARY_SHA256:
            return fallback                  # not the checked file: the browser refuses it too
        cached.parent.mkdir(parents=True, exist_ok=True)
        cached.write_bytes(code)
    code = cached.read_text(encoding="utf-8")
    return "<script>" + code.replace("</script", "<\\/script") + "</script>"


def build(db_path, out, *, limit=4000, with_text=True, inline_library=True) -> dict:
    nodes, links, terms = read_graph(db_path, limit, with_text)
    legend = regions(nodes, links, terms)
    if not with_text:
        for region in legend:
            region["words"] = []
    since = min((n["when"] for n in nodes if n["when"]), default="")[:10]
    data = {"nodes": nodes, "links": links, "regions": legend, "since": since,
            "timeline": read_timeline(db_path, limit, with_text), "textIncluded": with_text}
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    page = PAGE.replace("__DATA__", json.dumps(data, ensure_ascii=False).replace("<", "\\u003c"))
    out.write_text(page.replace("__LIBRARY__", _library_tag(inline_library)), encoding="utf-8")
    return {"file": str(out), "nodes": len(nodes), "links": len(links), "regions": len(legend)}


def main(argv=None):
    from turnbook.mcp_server import configured_db_path
    from turnbook.paths import turnbook_home
    parser = argparse.ArgumentParser(description="Open your memory as a 3D graph.")
    parser.add_argument("--limit", type=int, default=4000, help="most recent nodes to draw")
    parser.add_argument("--no-text", action="store_true",
                        help="leave conversation text and topic words out of the file")
    parser.add_argument("--out", default=None, help="where to write the page")
    parser.add_argument("--no-open", action="store_true")
    args = parser.parse_args(argv)
    result = build(configured_db_path(), args.out or turnbook_home() / "brain.html",
                   limit=args.limit, with_text=not args.no_text)
    print(f"{result['nodes']} memories, {result['links']} links, {result['regions']} regions -> "
          f"{result['file']}")
    if not args.no_open:
        webbrowser.open(Path(result["file"]).as_uri())


if __name__ == "__main__":
    main()
