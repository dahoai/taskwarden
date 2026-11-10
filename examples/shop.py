"""The offline shop demo, from the repo: `python examples/shop.py`.

The demo itself lives in the package (`taskwarden.demo`), so `pip install "taskwarden[dev]"`
and `taskwarden-demo` runs it without a clone. This file keeps the repo path working (CI runs
it, and the tests import `Reply` / `call` from here), and re-exports the rest to read.
"""
from taskwarden.demo import (  # noqa: F401
    ORDERS,
    TOOLS,
    Base,
    Message,
    Reply,
    ShopHost,
    Task,
    call,
    main,
    read_orders,
    refund,
    run,
    scripted,
)

if __name__ == "__main__":
    run()
