"""Command-line interface for SOFIA."""

import sys
from argparse import ArgumentParser
from importlib.metadata import version

from sofia.config import ConfigurationError, load_settings
from sofia.logging_setup import configure_logging


def main() -> int:
    """Parse arguments and initialize SOFIA before showing default help."""
    parser = ArgumentParser(prog="sofia", description="SOFIA desktop assistant")
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {version('project-sofia')}",
    )
    parser.parse_args()
    try:
        settings = load_settings()
    except ConfigurationError as error:
        print(f"sofia: {error}", file=sys.stderr)
        return 2
    configure_logging(settings)
    parser.print_help()
    return 0
