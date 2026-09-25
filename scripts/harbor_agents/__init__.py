"""Harbor agents kakashi carries itself (loaded by import path, see run_harbor.py).

The pinned PyPI Harbor release is never modified; an agent whose stock
implementation lacks something the harness needs is vendored here as a
module Harbor imports with `-a harbor_agents.<module>:<Class>`.  Each file
names the exact upstream it was copied from.
"""
