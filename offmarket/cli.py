"""Command line: `python -m offmarket run|serve|import|rescore|demo`."""
import argparse
import json
import logging
import os


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    p = argparse.ArgumentParser(prog="offmarket")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run", help="run the daily refresh now (use this from cron)")
    s = sub.add_parser("serve", help="start the website and the built-in 6am scheduler")
    s.add_argument("--host", default="0.0.0.0")
    s.add_argument("--port", type=int, default=8000)
    i = sub.add_parser("import", help="import a listings CSV export")
    i.add_argument("file")
    i.add_argument("--snapshot", action="store_true", help="file lists everything active today")
    sub.add_parser("rescore", help="recompute every lead's score")
    sub.add_parser("demo", help="load SAMPLE data (fake listings) to try the site; don't use on the real database")
    args = p.parse_args()

    from . import db, pipeline
    if args.cmd == "run":
        print(json.dumps(pipeline.run_daily(), indent=2, default=str))
    elif args.cmd == "serve":
        import uvicorn
        uvicorn.run("offmarket.web.app:app", host=args.host, port=int(os.getenv("PORT", args.port)))
    elif args.cmd == "import":
        from .sources import csv_import
        with open(args.file, "rb") as fh:
            rows, _ = csv_import.parse(fh.read(), os.path.basename(args.file))
        print(json.dumps(pipeline.import_rows(rows, args.snapshot, os.path.basename(args.file)), indent=2))
    elif args.cmd == "rescore":
        with db.engine().begin() as conn:
            print(pipeline.rescore(conn), "scored")
    elif args.cmd == "demo":
        from . import demo
        demo.load()


if __name__ == "__main__":
    main()
