#!/usr/bin/env python3
"""Compatibility entry point for existing exam_loop commands and Python callers."""
import sys
import telos

if __name__ == '__main__':
    try:
        telos.main()
    except (telos.GateError, OSError, ValueError, telos.subprocess.SubprocessError) as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
else:
    sys.modules[__name__] = telos
