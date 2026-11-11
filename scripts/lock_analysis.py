"""Capture all resolved analysis/build/test distributions and hashes for this platform.

Run only in a reviewed analysis environment. The local editable project is
installed separately with --no-deps --no-build-isolation after this lock.
"""
import email.parser
import argparse
import hashlib
import importlib.metadata
import json
from pathlib import Path
import platform
import subprocess
import sys
import tempfile
import zipfile


def main() -> None:
    root=Path(__file__).resolve().parents[1]
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir",type=Path,default=root)
    output=parser.parse_args().output_dir
    output.mkdir(parents=True,exist_ok=True)
    destination=output/"requirements-analysis.lock"
    info_path=output/"analysis-lock-info.json"
    if destination.exists() or info_path.exists():
        raise SystemExit("Refusing to overwrite an analysis lock; review/version any replacement explicitly")
    versions={d.metadata["Name"].lower().replace("_","-"):d.version for d in importlib.metadata.distributions()
              if d.metadata["Name"].lower()!="logquant"}
    entries={}
    with tempfile.TemporaryDirectory(prefix="logquant-analysis-wheels-") as temporary:
        subprocess.run([sys.executable,"-m","pip","download","--only-binary=:all:","--no-deps","--dest",temporary,
                        *[f"{name}=={version}" for name,version in sorted(versions.items())]],check=True)
        for path in sorted(Path(temporary).glob("*.whl")):
            with zipfile.ZipFile(path) as wheel:
                meta_name=next(n for n in wheel.namelist() if n.endswith(".dist-info/METADATA"))
                meta=email.parser.Parser().parsestr(wheel.read(meta_name).decode())
            name=meta["Name"].lower().replace("_","-")
            if versions.get(name)!=meta["Version"]:raise RuntimeError("Downloaded distribution mismatch")
            entries[name]={"version":meta["Version"],"wheel":path.name,"sha256":hashlib.sha256(path.read_bytes()).hexdigest()}
    if set(entries)!=set(versions):raise RuntimeError("Incomplete dependency artifact lock")
    text=("# Complete analysis + build + test lock, excluding the local editable LogQuant source.\n"
          f"# Target: CPython {platform.python_version()}, {sys.platform}/{platform.machine()}, macOS {platform.mac_ver()[0]}.\n"
          "# Install with pip --require-hashes --no-deps; wheels are platform-specific.\n")
    text+="".join(f"{name}=={entry['version']} --hash=sha256:{entry['sha256']}\n" for name,entry in sorted(entries.items()))
    with destination.open("x") as stream:stream.write(text)
    info={"python":platform.python_version(),"platform":sys.platform+"/"+platform.machine(),
          "macos":platform.mac_ver()[0],"packages":entries,"lock_sha256":hashlib.sha256(destination.read_bytes()).hexdigest()}
    with info_path.open("x") as stream:json.dump(info,stream,indent=2);stream.write("\n")
    print("Wrote complete analysis lock:",len(entries),"distributions",info["lock_sha256"])


if __name__=="__main__":main()
