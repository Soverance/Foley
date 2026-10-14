"""The only module allowed to modify files under the library root.

Every other module opens library files read-only; tests/test_readonly.py enforces
this with a runtime audit hook. Nothing here is implemented in Phase 1a: duplicate
cleanup (`foley dedupe apply / restore / purge`) arrives in Phase 2, CLI-only and
run by a human, following the safety rules in docs/handoffs/AudioCatalog.md.
"""

from __future__ import annotations
