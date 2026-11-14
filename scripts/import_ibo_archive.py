"""Parse the IBO 2013-2018 theory papers and load them into the archive tables.

    python scripts/import_ibo_archive.py                 # all papers
    python scripts/import_ibo_archive.py --only 2016-A 2016-B
    python scripts/import_ibo_archive.py --dry-run       # parse + report, no database
    python scripts/import_ibo_archive.py --no-assets     # skip figure crops (fast)
    python scripts/import_ibo_archive.py --report data/archive_report.md

Re-running replaces each exam it loads (questions, items, answers ...); figure
images are deduplicated by content hash. The database comes from
NBO_DATABASE_URL / DATABASE_URL, as for the app; run `alembic upgrade head`
first.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))

from app.archive.parse import parse_paper  # noqa: E402
from app.archive.sources import ALL_SPECS  # noqa: E402


def summarize(key, paper) -> dict:
    qs = paper.questions
    items = [i for q in qs for i in q.items]
    return {
        "paper": key,
        "questions": len(qs),
        "cancelled": sum(q.status == "cancelled" for q in qs),
        "items": len(items),
        "answered": sum(i.answer is not None for i in items),
        "explanations": sum(len(q.explanations) for q in qs),
        "figures": sum(1 for q in qs for e in q.elements if e.role == "figure")
        + sum(1 for q in qs for x in q.explanations for e in x.elements if e.role == "figure"),
        "tables": sum(1 for q in qs for e in q.elements if e.role == "table"),
        "needs_review": [q.number for q in qs if any(not n.startswith("blacked out") for n in q.notes)],
        "formats": sorted({q.format for q in qs}),
    }


def write_report(path: Path, rows: list[dict], papers: dict):
    out = ["# IBO archive import report", "",
           "| paper | questions | cancelled | items | answered | explanations | figures | tables | review |",
           "|---|---|---|---|---|---|---|---|---|"]
    for r in rows:
        out.append(f"| {r['paper']} | {r['questions']} | {r['cancelled']} | {r['items']} | {r['answered']} | "
                   f"{r['explanations']} | {r['figures']} | {r['tables']} | {len(r['needs_review'])} |")
    out += ["", "## Questions flagged for review", ""]
    for key, paper in papers.items():
        flagged = [q for q in paper.questions if any(not n.startswith("blacked out") for n in q.notes)]
        if not flagged:
            continue
        out.append(f"### {key}")
        for q in flagged:
            out.append(f"- **{q.label}** (p. {q.page_start}-{q.page_end}): " + "; ".join(q.notes))
        out.append("")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(out), encoding="utf-8")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--only", nargs="*", help="paper keys, e.g. 2013-A 2017-2")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-assets", action="store_true")
    ap.add_argument("--dpi", type=int, default=150)
    ap.add_argument("--report", type=Path)
    args = ap.parse_args()

    keys = args.only or list(ALL_SPECS)
    unknown = [k for k in keys if k not in ALL_SPECS]
    if unknown:
        ap.error(f"unknown paper(s) {unknown}; choose from {list(ALL_SPECS)}")

    db = None
    if not args.dry_run:
        from app.archive.load import load_paper
        from app.db import SessionLocal
        db = SessionLocal()

    rows, papers = [], {}
    try:
        for key in keys:
            t0 = time.time()
            paper = parse_paper(ALL_SPECS[key](), BASE)
            papers[key] = paper
            r = summarize(key, paper)
            rows.append(r)
            if db is not None:
                load_paper(db, paper, BASE, with_assets=not args.no_assets, dpi=args.dpi)
                db.commit()
            print(f"{key}: {r['questions']} questions ({r['cancelled']} cancelled), {r['items']} items, "
                  f"{r['answered']} answered, {r['explanations']} explanations, {r['figures']} figures, "
                  f"{len(r['needs_review'])} to review [{time.time() - t0:.1f}s]", flush=True)
        if db is not None:
            # crops of replaced exams that nothing points at any more
            from sqlalchemy import text
            n = db.execute(text(
                "DELETE FROM ibo_assets WHERE id NOT IN (SELECT asset_id FROM ibo_question_elements "
                "WHERE asset_id IS NOT NULL)")).rowcount
            db.commit()
            if n:
                print(f"removed {n} unreferenced images")
    except Exception:
        if db is not None:
            db.rollback()
        raise
    finally:
        if db is not None:
            db.close()
    if args.report:
        write_report(args.report, rows, papers)
        print(f"report written to {args.report}")


if __name__ == "__main__":
    main()
