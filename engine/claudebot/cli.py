"""The `cb` command line. Each module adds its commands via register()."""
import argparse
import sys

from . import (archive, bots, dispatcher, items, messages, requests, routines,
               search, store)

MODULES = [store, bots, items, messages, requests, search, routines,
           dispatcher, archive]


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="cb")
    sub = p.add_subparsers(dest="command", required=True)
    for m in MODULES:
        m.register(sub)
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
