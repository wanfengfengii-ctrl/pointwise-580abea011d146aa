import json
from tempfile import TemporaryDirectory

from archive.initialize import initialize
from archive.publish import AFTER_PREPARE, BEFORE_COMMIT, sign
from archive.readback import read
from audit.shift import audit
from ledger.cancel import cancel
from review.candidates import RequestSlot
from review.criteria import Gauge, SlotRequirement
from review.freeze import freeze
from source.ingest import ingest
from source.origins import Origin
from tests.support import stop_at


def load_case(stores):
    refs = {}
    for line, (name, reading) in enumerate((("A", 0), ("B", 7), ("M", None), ("D", 5), ("C", 6)), 1):
        refs[name] = ingest(stores, "P1", name, unit="um", reading=reading,
                            category="probe", origin=Origin("head", "shift.csv", line))
    a, b, m, d, c = (refs[name] for name in ("A", "B", "M", "D", "C"))
    freeze(stores, "day", (a, b, m, d), Gauge(0, 10), (
        SlotRequirement("U", ("probe",), (b, m)),
        SlotRequirement("V", ("probe",), (a,)),
    ))
    neighbor = (("N2", d), ("N0", c), ("N1", a))
    freeze(stores, "neighbor", (a, c, d), Gauge(0, 10), tuple(
        SlotRequirement(slot, ("probe",), (ref,)) for slot, ref in neighbor
    ))
    x = tuple(RequestSlot(slot, (ref,)) for slot, ref in neighbor)
    y = (RequestSlot("U", (b,)),)
    return refs, x, y


def observe_v1(stores, *, reverse=False):
    _, x, y = load_case(stores)
    opening = audit(stores, "day")["balance"]
    held = sign(stores, "neighbor", "X", x, checkpoint=stop_at(AFTER_PREPARE))
    overlap = audit(stores, "day")["balance"]
    if reverse:
        prepared = sign(stores, "day", "Y", y, checkpoint=stop_at(AFTER_PREPARE))
        returned = cancel(stores, "neighbor", "X", x)
    else:
        returned = cancel(stores, "neighbor", "X", x)
        prepared = sign(stores, "day", "Y", y, checkpoint=stop_at(AFTER_PREPARE))
    at_close = audit(stores, "day")["balance"]
    rolled_back = sign(stores, "day", "Y", y, checkpoint=stop_at(BEFORE_COMMIT))
    after_rollback = audit(stores, "day")["balance"]
    signed = sign(stores, "day", "Y", y)
    final = audit(stores, "day")["balance"]
    if [result["status"] for result in (held, prepared, returned, rolled_back, signed)] != [
        "pending", "pending", "cancelled", "pending", "signed",
    ] or after_rollback != at_close or read(stores, "R-Y") != signed:
        raise AssertionError("V1 hold, cancel, rollback or issue differed from the sample")
    actual = [opening, overlap, at_close, final]
    expected = [
        {"used": 0, "available": 2, "held": 0, "missing": 1, "ineligible": 1, "unconfirmed": 0},
        {"used": 0, "available": 1, "held": 2, "missing": 1, "ineligible": 0, "unconfirmed": 0},
        {"used": 0, "available": 1, "held": 1, "missing": 1, "ineligible": 1, "unconfirmed": 0},
        {"used": 1, "available": 1, "held": 0, "missing": 1, "ineligible": 1, "unconfirmed": 0},
    ]
    if actual != expected:
        raise AssertionError(f"Unexpected V1 balances: {actual!r}")
    return dict(zip(("opening", "overlap", "before_issue", "after_issue"), actual, strict=True))


def main():
    with TemporaryDirectory() as directory:
        print(json.dumps(observe_v1(initialize(directory)), indent=2))


if __name__ == "__main__":
    main()
