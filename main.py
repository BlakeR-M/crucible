"""Entry point.

This file exists to stop a subtle and expensive bug rather than for tidiness.

Started as `python -m crucible.server`, the module runs under the name
`__main__`, and anything that later does `from . import server` imports a
*second* copy of it with its own module-level state. The server then holds two
run registries: the web layer reads one while the chat agent starts runs in the
other, so a review executes correctly, writes its ledger, and is invisible to
the page that asked for it.

Importing the module by its real name first means there is only ever one.

    python main.py
"""

from crucible.server import serve

if __name__ == "__main__":
    serve()
