import os
import sys
import json
import numpy as np

def is_valid_frame(frame):
    return (any(f != 0 for f in frame[:3]) and frame[3] > -155)

def report_fingerprint_health(folder):
    files = [f for f in os.listdir(folder) if f.endswith('.json')]
    for fname in sorted(files):
        path = os.path.join(folder, fname)
        with open(path, 'r', encoding='utf-8') as f:
            data = json.load(f)
        frames = data.get('fingerprint', [])
        total = len(frames)
        valid = sum(is_valid_frame(frame) for frame in frames)
        pct = 100.0 * valid / total if total else 0.0
        health = 'HEALTHY' if pct >= 75 else 'UNHEALTHY'
        highlight = ' <<<' if pct < 75 else ''
        print(f"{fname}: {valid} / {total} valid frames ({pct:.1f}%) [{health}]{highlight}")

if __name__ == '__main__':
    folder = sys.argv[1] if len(sys.argv) > 1 else '.'
    report_fingerprint_health(folder) 