"""`python -m fleet` → the SPEC §10 command surface (ADR-0015).

The Typer import stays behind `main()`: `import fleet` is done by every worker and every test in
the state layer, and none of them should pull in a CLI framework to do it. `fleet/cli.py` imports
Typer at module scope because it *is* the CLI boundary — this file is what keeps that boundary
from leaking into `import fleet`.
"""

from __future__ import annotations


def main() -> None:
    from fleet.cli import main as cli_main

    cli_main()


if __name__ == "__main__":
    main()
