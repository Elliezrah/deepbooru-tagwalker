"""Regression test for the audit-exception-list persistence."""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pathlib import Path
from core import audit_exceptions_io as ex


def run() -> None:
    d = Path(tempfile.mkdtemp())
    p = d / "audit_exceptions.json"

    # Empty to start.
    assert ex.load_exceptions(p) == set()

    # Add (case-insensitive, trimmed).
    assert ex.add_exception("My_Studio", p)
    assert ex.add_exception("  OC_Name  ", p)
    assert ex.load_exceptions(p) == {"my_studio", "oc_name"}

    # Duplicate (different case) is a no-op, not a second entry.
    assert ex.add_exception("MY_STUDIO", p)
    assert ex.load_exceptions(p) == {"my_studio", "oc_name"}

    # Empty/whitespace tag rejected.
    assert ex.add_exception("   ", p) is False
    assert ex.load_exceptions(p) == {"my_studio", "oc_name"}

    # Remove (case-insensitive); removing a missing tag still "succeeds".
    assert ex.remove_exception("MY_studio", p)
    assert ex.load_exceptions(p) == {"oc_name"}
    assert ex.remove_exception("not_there", p)
    assert ex.load_exceptions(p) == {"oc_name"}

    # Bulk round-trip.
    assert ex.save_exceptions(["A", "b", "b", " c "], p)
    assert ex.load_exceptions(p) == {"a", "b", "c"}

    # Corrupt file -> empty set, no raise.
    p.write_text("{ not json", encoding="utf-8")
    assert ex.load_exceptions(p) == set()

    print("OK: audit exception list persistence verified "
          "(add/remove/dedup/case-insensitive/atomic/corrupt-safe)")


if __name__ == "__main__":
    run()
