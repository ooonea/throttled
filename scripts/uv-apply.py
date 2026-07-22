#!/usr/bin/env python3
"""Apply and read back an undervolt offset using throttled's own encoding.

Reuses calc_undervolt_msr/writemsr from the repo so the test harness and the
daemon can never disagree on the MSR 150h encoding. Root required.

Usage:
    uv-apply.py <mV> [PLANE...]    apply offset (mV <= 0; default CORE CACHE)
    uv-apply.py --read             print the current offsets, one plane per line

Exit codes: 0 ok, 2 read-back mismatch (offset was not accepted).
"""

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
# throttled.py does `from mmio import MMIO`: the repo root must be importable
# (pytest adds it for the test suite, standalone use must do it itself)
sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location('throttled', ROOT / 'throttled.py')
throttled = importlib.util.module_from_spec(spec)
spec.loader.exec_module(throttled)
throttled.args = SimpleNamespace(log=None, debug=False, config=None, monitor=None)


def main():
    if len(sys.argv) < 2:
        sys.exit(__doc__)

    if sys.argv[1] == '--read':
        for plane, offset_mv in throttled.get_undervolt(convert=True).items():
            print(f'{plane} {offset_mv:.0f}')
        return

    offset_mv = float(sys.argv[1])
    planes = sys.argv[2:] or ['CORE', 'CACHE']
    if offset_mv > 0:
        sys.exit('refusing a positive offset: this tool only undervolts')

    for plane in planes:
        throttled.writemsr('MSR_OC_MAILBOX', throttled.calc_undervolt_msr(plane, offset_mv))
        read_mv = throttled.get_undervolt(plane, convert=True)[plane]
        print(f'{plane} applied {offset_mv:.0f} mV, read back {read_mv:.0f} mV')
        if abs(read_mv - offset_mv) > 2:
            sys.exit(2)


if __name__ == '__main__':
    main()
