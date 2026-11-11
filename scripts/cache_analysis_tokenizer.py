"""Download one pinned public vocabulary, never call a model. Runtime is cache-only."""
import hashlib
import argparse
from pathlib import Path
import urllib.request

URL = "https://openaipublic.blob.core.windows.net/encodings/o200k_base.tiktoken"
SHA = "446a9538cb6c348e3516120d7c08b09f57c36495e2acfffe59a5bf8b0cfb1a2d"

if __name__ == "__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir",type=Path,default=Path(__file__).resolve().parents[1]/".analysis-cache/tiktoken")
    directory = parser.parse_args().output_dir
    directory.mkdir(parents=True,exist_ok=True)
    path = directory/hashlib.sha1(URL.encode()).hexdigest()
    if path.exists():
        if hashlib.sha256(path.read_bytes()).hexdigest() != SHA: raise SystemExit("Existing tokenizer hash mismatch; refusing overwrite")
    else:
        data = urllib.request.urlopen(URL,timeout=30).read()
        if hashlib.sha256(data).hexdigest() != SHA: raise SystemExit("Tokenizer hash mismatch")
        with path.open("xb") as stream: stream.write(data)
    print("Verified tokenizer",SHA)
