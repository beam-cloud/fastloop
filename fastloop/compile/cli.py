# fastloop/compile/cli.py
import argparse
from pathlib import Path

from .compiler import FastLoopCompiler


def main():
    parser = argparse.ArgumentParser(description="Compile FastLoop app for Pyodide")
    parser.add_argument("input", help="Path to your FastLoop Python file")
    parser.add_argument(
        "-o", "--output", default="fastloop_bundle.zip", help="Output zip file"
    )

    args = parser.parse_args()

    source_code = Path(args.input).read_text()
    compiler = FastLoopCompiler()
    bundle_bytes = compiler.compile_to_pyodide_bundle(source_code)

    Path(args.output).write_bytes(bundle_bytes)
    print(f"✅ Bundle created at {args.output}")


if __name__ == "__main__":
    main()
