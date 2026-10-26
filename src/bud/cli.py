import argparse

from bud import __version__


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="bud", description="bud, a personal AI teammate.")
    parser.add_argument("--version", action="version", version=f"bud {__version__}")
    parser.parse_args(argv)
    parser.print_help()
