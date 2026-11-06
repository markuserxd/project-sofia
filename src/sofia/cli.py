"""Command-line interface for SOFIA."""

from argparse import ArgumentParser
from importlib.metadata import version


def main() -> int:
    """Parse command-line arguments and show help when no option is given."""
    parser = ArgumentParser(prog="sofia", description="SOFIA desktop assistant")
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {version('project-sofia')}",
    )
    parser.parse_args()
    parser.print_help()
    return 0
