"""STEP -> binary STL, in whichever Python has CadQuery.

Run as a subprocess by models.py: the assistant's own environment does not
carry CadQuery (it is a 700 MB OCCT build), but the FTC project's does, so the
conversion is delegated to that interpreter. Argument order: src dst [tolerance].
"""
import sys


def main() -> int:
    src, dst = sys.argv[1], sys.argv[2]
    tol = float(sys.argv[3]) if len(sys.argv) > 3 else 0.6
    import cadquery as cq
    shape = cq.importers.importStep(src)
    cq.exporters.export(shape, dst, tolerance=tol, angularTolerance=0.3,
                        exportType="STL", opt={"ascii": False})
    return 0


if __name__ == "__main__":
    sys.exit(main())
