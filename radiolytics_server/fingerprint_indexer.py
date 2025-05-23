import os
import json
import csv
import argparse
from datetime import datetime

FINGERPRINT_ROOT = os.getenv("FINGERPRINT_OUTPUT_PATH", "ADMIN DO NOT COMMIT/fingerprints/")
INDEX_CSV = "fingerprint_index.csv"


def index_fingerprints(root_dir=FINGERPRINT_ROOT, out_csv=INDEX_CSV):
    """Index all fingerprints in the given directory and output a CSV summary."""
    rows = []
    for root, dirs, files in os.walk(root_dir):
        for f in files:
            if f.endswith('.json'):
                path = os.path.join(root, f)
                try:
                    with open(path) as fp:
                        data = json.load(fp)
                    row = {
                        'file': os.path.relpath(path, root_dir),
                        'type': 'app' if 'device_id' in data else 'reference',
                        'station_or_device': data.get('station') or data.get('device_id'),
                        'timestamp': data.get('timestamp'),
                        'datetime': datetime.fromtimestamp(data.get('timestamp', 0)).isoformat() if data.get('timestamp') else '',
                        'length': len(data.get('fingerprint', []))
                    }
                    rows.append(row)
                except Exception as e:
                    print(f"Error reading {path}: {e}")
    if rows:
        with open(out_csv, 'w', newline='') as out:
            writer = csv.DictWriter(out, fieldnames=rows[0].keys())
            writer.writeheader()
            writer.writerows(rows)
        print(f"Wrote {out_csv} with {len(rows)} fingerprints.")
    else:
        print("No fingerprints found.")


def main():
    parser = argparse.ArgumentParser(description="Radiolytics Fingerprint Indexer")
    parser.add_argument('--index', action='store_true', help='Index all fingerprints and output CSV summary')
    parser.add_argument('--root', type=str, default=FINGERPRINT_ROOT, help='Root directory to scan')
    parser.add_argument('--csv', type=str, default=INDEX_CSV, help='Output CSV file')
    args = parser.parse_args()

    if args.index:
        index_fingerprints(args.root, args.csv)
    else:
        parser.print_help()

if __name__ == "__main__":
    main() 