import os
import time
import json
import numpy as np
import firebase_admin
from firebase_admin import credentials, storage, firestore
import logging
from datetime import datetime
import threading
from collections import deque
from dotenv import load_dotenv
import csv
from typing import List, Tuple, Optional, Dict, Any
import argparse
import pandas as pd

"""
Radiolytics Fingerprint Matcher

Fingerprint JSON format:
{
    "fingerprint": [[RMS, centroid, energy, dB], ...],
    "timestamp": <int>,
    "device_id" or "station": <str>
}

Expected flow:
- Load reference and app fingerprints (N x 4 float arrays)
- For each app fingerprint, slide over reference fingerprints using a window/cross-correlation
- Find the best alignment (offset) and similarity
- Log successful matches to CSV
- Configurable thresholds via config.json or CLI
"""

# --- CONFIGURATION ---
CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'config.json')
with open(CONFIG_PATH, 'r') as f:
    config = json.load(f)
POLL_INTERVAL = config.get('POLL_INTERVAL', 5)
MATCH_THRESHOLD = config.get('MATCH_THRESHOLD', 0.75)
MIN_FRAMES_MATCH = config.get('MIN_FRAMES_MATCH', 10)
BUFFER_MINUTES = config.get('BUFFER_MINUTES', 3)
LOG_CSV = config.get('LOG_CSV', 'successful_matches.csv')

# --- LOGGING SETUP ---
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('fingerprint_matcher.log'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# --- FIREBASE SETUP ---
load_dotenv()

cred_path = os.getenv("GOOGLE_APPLICATION_CREDENTIALS")
cred = credentials.Certificate(cred_path)
firebase_admin.initialize_app(cred, {
    'storageBucket': 'newbuckettest.firebasestorage.app'
})
bucket = storage.bucket()
db = firestore.client()

# Update all local fingerprint file output to use FINGERPRINT_OUTPUT_PATH from .env
FINGERPRINT_OUTPUT_PATH = os.getenv("FINGERPRINT_OUTPUT_PATH", "ADMIN DO NOT COMMIT/fingerprints/")

class FingerprintMatcher:
    def __init__(self):
        """Initialize matcher, load config, and set up logging."""
        self.processed_files = set()
        self.reference_fingerprints: Dict[str, List[Tuple[int, str, List[List[float]]]]] = {}
        self.MATCH_THRESHOLD = MATCH_THRESHOLD
        self.MIN_FRAMES_MATCH = MIN_FRAMES_MATCH
        self.BUFFER_MINUTES = BUFFER_MINUTES
        self.is_running = False
        self.thread = None
        self.log_csv = LOG_CSV
        
        # Initialize reference fingerprint buffers for each station
        self._load_station_config()
        
        # Use module-level Firebase instances
        self.bucket = bucket
        self.db = db
        
        # Create processed_fingerprints directory if it doesn't exist
        os.makedirs('processed_fingerprints', exist_ok=True)
        
        logger.info("Initialized FingerprintMatcher with threshold %.2f and min frames %d", 
                   self.MATCH_THRESHOLD, self.MIN_FRAMES_MATCH)

    def _load_station_config(self):
        """Load station names from config.json"""
        try:
            with open('config.json', 'r') as f:
                config = json.load(f)
            for station in config.get('stations', {}).keys():
                self.reference_fingerprints[station] = []
        except Exception as e:
            logger.error(f"Error loading config: {str(e)}")

    def start(self):
        if self.is_running:
            return
            
        self.is_running = True
        self.thread = threading.Thread(target=self._match_loop, daemon=True)
        self.thread.start()
        logger.info("Started fingerprint matcher")

    def stop(self):
        self.is_running = False
        if self.thread:
            self.thread.join()
        logger.info("Stopped fingerprint matcher")

    def _match_loop(self):
        """Main loop that polls for new fingerprints and processes them"""
        while self.is_running:
            try:
                # Load new reference fingerprints
                self._load_reference_fingerprints()
                
                # Process new incoming fingerprints
                self._process_incoming_fingerprints()
                
                time.sleep(POLL_INTERVAL)
                
            except Exception as e:
                logger.error(f"Error in match loop: {str(e)}")
                time.sleep(5)  # Wait before retrying

    def _load_reference_fingerprints(self):
        """Load new reference fingerprints from Firebase Storage"""
        try:
            cutoff_time = int(time.time()) - (self.BUFFER_MINUTES * 60)
            
            # List all fingerprints (reference and app) from the root 'fingerprints/' folder
            blobs = self.bucket.list_blobs(prefix="fingerprints/")
            for blob in blobs:
                if not blob.name.endswith('.json'):
                    continue
                    
                # Extract timestamp and station from filename
                try:
                    filename = blob.name.split('/')[-1]
                    parts = filename.replace('LiveStreamFingerprint_', '').split('_')
                    timestamp = int(parts[0])
                    station = '_'.join(parts[1:]).replace('.json', '')
                except (ValueError, IndexError):
                    continue
                    
                # Skip if too old
                if timestamp < cutoff_time:
                    continue
                    
                # Skip if already in buffer
                if any(ref_ts == timestamp and ref_st == station for ref_ts, ref_st, _ in self.reference_fingerprints.get(station, [])):
                    continue
                
                # Download and parse
                try:
                    data = json.loads(blob.download_as_text())
                    fingerprint = data.get('fingerprint')
                    if fingerprint:
                        if station not in self.reference_fingerprints:
                            self.reference_fingerprints[station] = []
                        self.reference_fingerprints[station].append((timestamp, station, fingerprint))
                        logger.info(f"Loaded reference fingerprint for {station} at {timestamp}")
                except Exception as e:
                    logger.error(f"Error loading reference fingerprint {blob.name}: {str(e)}")
            
            # Remove old fingerprints from each station's buffer
            for station in self.reference_fingerprints:
                self.reference_fingerprints[station] = [
                    (ref_ts, ref_st, ref_fp) for ref_ts, ref_st, ref_fp in self.reference_fingerprints[station]
                    if ref_ts >= cutoff_time
                ]
            
        except Exception as e:
            logger.error(f"Error loading reference fingerprints: {str(e)}")

    def _process_incoming_fingerprints(self):
        """Process new incoming fingerprints from Firebase Storage"""
        try:
            # List all incoming fingerprints with AppFingerprint_ prefix
            blobs = [blob for blob in self.bucket.list_blobs(prefix="incoming_fingerprints/")
                     if blob.name.endswith('.json') and os.path.basename(blob.name).startswith('AppFingerprint_')]

            # Group blobs by device_id (parsed from file content)
            device_files = {}
            for blob in blobs:
                try:
                    data = json.loads(blob.download_as_text())
                    device_id = data.get('device_id')
                    timestamp = data.get('timestamp')
                    if not device_id or not timestamp:
                        logger.warning(f"Missing device_id or timestamp in {blob.name}")
                        continue
                    if device_id not in device_files:
                        device_files[device_id] = []
                    device_files[device_id].append((timestamp, blob, data))
                except Exception as e:
                    logger.error(f"Error reading blob {blob.name}: {str(e)}")
                    continue

            # For each device, process only the most recent file
            for device_id, files in device_files.items():
                # Sort by timestamp descending
                files_sorted = sorted(files, key=lambda x: x[0], reverse=True)
                newest = files_sorted[0]
                timestamp, blob, data = newest
                if blob.name in self.processed_files:
                    continue
                uploaded_fp = data.get('fingerprint')
                uploaded_ts = data.get('timestamp')
                # Find best match
                best_match = self._find_best_match(uploaded_fp)
                if best_match:
                    match_ts, match_station, similarity, offset = best_match
                    logger.info(f"Best match: station={match_station}, ts={match_ts}, sim={similarity:.3f}, offset={offset}")
                    try:
                        self._log_successful_match(os.path.basename(blob.name), match_station, similarity, offset, len(uploaded_fp), offset, datetime.now().isoformat())
                    except Exception as e:
                        logger.error(f"Error logging successful match: {e}")
                    # Write result to Firestore so the app can display the correct match
                    result = {
                        "matched_at": int(time.time() * 1000),
                        "match_timestamp": match_ts,
                        "station": match_station,
                        "confidence": float(similarity)
                    }
                    self.db.collection("results").document(str(uploaded_ts)).set(result)
                    logger.info(f"Wrote match result to Firestore for {os.path.basename(blob.name)}")
                else:
                    result = {
                        "matched_at": int(time.time() * 1000),
                        "station": "Unknown",
                        "confidence": 0.0
                    }
                    self.db.collection("results").document(str(uploaded_ts)).set(result)
                    logger.info(f"No match found for {os.path.basename(blob.name)}")
                self.processed_files.add(blob.name)
                # Save locally (only for AppFingerprint_ prefix)
                if os.path.basename(blob.name).startswith('AppFingerprint_'):
                    local_dir = FINGERPRINT_OUTPUT_PATH
                    os.makedirs(local_dir, exist_ok=True)
                    local_path = os.path.join(local_dir, os.path.basename(blob.name))
                    with open(local_path, 'w') as f:
                        f.write(blob.download_as_text())
                    logger.info(f"Saved incoming fingerprint locally at {local_path}")
                # Move to fingerprints (root folder)
                try:
                    new_blob_name = f"fingerprints/{os.path.basename(blob.name)}"
                    new_blob = self.bucket.blob(new_blob_name)
                    new_blob.content_type = 'application/json'
                    new_blob.content_disposition = 'attachment; filename="' + os.path.basename(blob.name) + '"'
                    new_blob.upload_from_string(blob.download_as_text(), content_type='application/json')
                    new_blob.make_public()
                    public_url = new_blob.public_url
                    logger.info(f"Moved {blob.name} to {new_blob_name} - Public URL: {public_url}")
                    blob.delete()
                except Exception as e:
                    logger.error(f"Error moving fingerprint file {blob.name}: {str(e)}")
                    continue
                # Cleanup: keep only 10 most recent AppFingerprint_*.json per device
                if len(files_sorted) > 10:
                    for old in files_sorted[10:]:
                        old_blob = old[1]
                        try:
                            old_blob.delete()
                            logger.info(f"Deleted old Firebase fingerprint for device {device_id}: {old_blob.name}")
                        except Exception as e:
                            logger.error(f"Failed to delete old Firebase file {old_blob.name}: {str(e)}")
        except Exception as e:
            logger.error(f"Error processing incoming fingerprints: {e}")
            logger.error("Full error details:", exc_info=True)

    def _find_best_match(self, uploaded_fp: List[List[float]]) -> Optional[Tuple[int, str, float, int]]:
        """
        Find the best matching reference fingerprint using sliding window/cross-correlation.
        Returns (timestamp, station, similarity, offset) for the best match.
        """
        try:
            best_match = None
            best_similarity = 0.0
            best_offset = 0
            best_station = None
            uploaded_fp = np.array(uploaded_fp)
            if len(uploaded_fp.shape) == 1:
                uploaded_fp = uploaded_fp.reshape(1, -1)
            for station, fingerprints in self.reference_fingerprints.items():
                for ref_ts, ref_st, ref_fp in fingerprints:
                    ref_fp = np.array(ref_fp)
                    if len(ref_fp.shape) == 1:
                        ref_fp = ref_fp.reshape(1, -1)
                    # Sliding window: slide uploaded_fp over ref_fp
                    n = len(ref_fp)
                    m = len(uploaded_fp)
                    if m > n:
                        continue  # Can't match if query is longer than reference
                    for offset in range(n - m + 1):
                        window = ref_fp[offset:offset + m]
                        # Cosine similarity per frame, then mean
                        sims = [np.dot(u / (np.linalg.norm(u) + 1e-10), w / (np.linalg.norm(w) + 1e-10))
                                for u, w in zip(uploaded_fp, window)]
                        avg_sim = np.mean(sims)
                        if avg_sim > best_similarity:
                            best_similarity = avg_sim
                            best_match = (ref_ts, station, avg_sim, offset)
            if best_match and best_similarity >= self.MATCH_THRESHOLD:
                logger.info(f"Best match: station={best_match[1]}, ts={best_match[0]}, sim={best_match[2]:.3f}, offset={best_match[3]}")
                return best_match
            else:
                logger.info("No match found above threshold")
                return None
        except Exception as e:
            logger.error(f"Error in find_best_match: {str(e)}")
            return None

    def _log_successful_match(self, query_filename, reference_filename, similarity, offset, recording_length, time_offset, dt):
        """Log a successful match to the CSV log."""
        try:
            with open(self.log_csv, 'a') as f:
                f.write(f"{query_filename},{reference_filename},{similarity},{offset},{recording_length},{time_offset},{dt}\n")
        except Exception as e:
            logger.error(f"Error writing to log CSV: {e}")

def analyze_log(log_csv: str):
    """
    Analyze the match log and print trends, best recording lengths, offsets, and station reliability.
    Print actionable suggestions based on log data.
    """
    try:
        df = pd.read_csv(log_csv, header=None, names=[
            'query_filename', 'reference_filename', 'similarity', 'offset', 'recording_length', 'time_offset', 'datetime'])
        if df.empty:
            print("No matches logged yet.")
            return
        print("\n=== Match Log Analytics ===")
        print(f"Total matches: {len(df)}")
        # Best average similarity by recording length
        best_len = df.groupby('recording_length')['similarity'].mean().idxmax()
        print(f"Best recording length (avg similarity): {best_len} frames")
        # Best average similarity by offset
        best_offset = df.groupby('offset')['similarity'].mean().idxmax()
        print(f"Best offset (avg similarity): {best_offset} frames")
        # Most reliably matched station
        best_station = df.groupby('reference_filename')['similarity'].mean().idxmax()
        print(f"Most reliably matched station: {best_station}")
        # Suggestion
        print(f"\nSuggestion: Try recording {best_len / 8:.1f} seconds for best results (assuming 8 frames/sec)")
        print("==========================\n")
    except Exception as e:
        print(f"Error analyzing log: {e}")

def load_json_file(path: str) -> Any:
    """Utility to load a JSON file with error handling."""
    try:
        with open(path, 'r') as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"Error loading JSON file {path}: {str(e)}")
        return None

def save_json_file(path: str, data: Any) -> bool:
    """Utility to save a JSON file with error handling."""
    try:
        with open(path, 'w') as f:
            json.dump(data, f)
        return True
    except Exception as e:
        logger.error(f"Error saving JSON file {path}: {str(e)}")
        return False

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Radiolytics Fingerprint Matcher CLI")
    parser.add_argument('--analyze-log', action='store_true', help='Analyze the match log and print trends')
    parser.add_argument('--log-csv', type=str, default=LOG_CSV, help='Path to match log CSV')
    parser.add_argument('--run-matcher', action='store_true', help='Run the fingerprint matcher service')
    args = parser.parse_args()

    if args.analyze_log:
        try:
            analyze_log(args.log_csv)
        except Exception as e:
            print(f"Error running log analysis: {e}")
    elif args.run_matcher:
        try:
            matcher = FingerprintMatcher()
            matcher.start()
            print("Matcher service started. Press Ctrl+C to stop.")
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            logger.info("Shutting down...")
            print("Matcher service stopped.")
        except Exception as e:
            logger.error(f"Error running matcher: {str(e)}")
            print(f"Error running matcher: {e}")
        finally:
            matcher.stop()
    else:
        print("No valid command provided. Use --help for usage information.")
        parser.print_help() 