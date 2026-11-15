"""Print a compact parse report for one archive paper: python scripts/archive_debug.py 2013-A [qnum]"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.archive.parse import parse_paper  # noqa: E402
from app.archive.sources import ALL_SPECS  # noqa: E402

BASE = Path(__file__).resolve().parent.parent
key = sys.argv[1]
only = int(sys.argv[2]) if len(sys.argv) > 2 else None
paper = parse_paper(ALL_SPECS[key](), BASE)
print(f"{key}: {len(paper.questions)} questions; review: {sum(q.needs_review for q in paper.questions)}")
for q in paper.questions:
    if only is not None and q.number != only:
        continue
    roles = {}
    for e in q.elements:
        roles[e.role] = roles.get(e.role, 0) + 1
    ans = "".join((("T" if i.answer.get("bool") else "F") if i.answer and "bool" in i.answer else (i.answer_text or "?")) for i in q.items)
    print(f"Q{q.number:>3} p{q.page_start}-{q.page_end} {q.format:14s} items={len(q.items)} ans={ans:6s} expl={len(q.explanations)} el={roles} sec={q.section!r} title={q.title!r} {q.status}")
    for n in q.notes:
        print("      !", n)
    if only is not None:
        for e in q.elements:
            print(f"   [{e.role}{'->'+str(e.parent) if e.parent is not None else ''}] {e.plain[:300]!r} {e.attrs.get('labels', '')[:8] if e.role=='figure' else ''}")
        for i in q.items:
            print(f"   ({i.label}) {i.item_type} {i.plain[:300]!r} -> {i.answer_text} [{i.answer_source}] {i.options or ''}")
        for x in q.explanations:
            print(f"   <{x.kind}:{x.item_label}> {x.plain[:300]!r} figs={len(x.elements)}")
