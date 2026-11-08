"""Separate GUI entrypoint; the existing CLI remains independent of Qt."""

from sofia.ui.app import main

raise SystemExit(main())
