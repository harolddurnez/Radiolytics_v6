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
import subprocess
import sys

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
LOG_FILE = 'radiolytics_server.log'  # Single consolidated log file
OLD_LOG_FILES = ['fingerprint_matcher.log', 'reference_recorder.log']  # Old log files to clean up

# Clear the log file when server starts and remove old log files
def clear_log_file():
    """Clear the log file at server start and remove old log files."""
    try:
        # Delete old log files
        for old_log in OLD_LOG_FILES:
            try:
                if os.path.exists(old_log):
                    os.remove(old_log)
                    print(f"Deleted old log file: {old_log}")
            except Exception as e:
                print(f"Error deleting {old_log}: {e}")
        
        # Clear and initialize new log file
        with open(LOG_FILE, 'w') as f:
            f.write(f"=== Radiolytics Server Log ===\n")
            f.write(f"Started at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write("="*80 + "\n\n")
        logger.info("Log file cleared and initialized")
    except Exception as e:
        print(f"Error clearing log file: {e}")

# Custom formatter to add separators between report instances
class ReportSeparatorFormatter(logging.Formatter):
    def __init__(self, fmt=None, datefmt=None):
        super().__init__(fmt, datefmt)
        self.last_report_time = None
        self.separator = "\n" + "="*80 + "\n"  # 80-character separator line

    def format(self, record):
        # Add separator between different report instances (fingerprint processing)
        if hasattr(record, 'fingerprint_id'):
            current_time = time.time()
            if self.last_report_time is None or (current_time - self.last_report_time) > 1.0:
                # New report instance
                self.last_report_time = current_time
                return self.separator + super().format(record)
        return super().format(record)

# Set up logging with the custom formatter
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s | %(levelname)-8s | %(message)s',
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler()
    ]
)

# Apply custom formatter to all handlers
formatter = ReportSeparatorFormatter('%(asctime)s | %(levelname)-8s | %(message)s')
for handler in logging.getLogger().handlers:
    handler.setFormatter(formatter)

logger = logging.getLogger(__name__)

# Add custom log formatters for different types of messages
def format_match_log(app_fp: str, ref_ts: int, station: str, similarity: float, offset: int, is_match: bool = False) -> str:
    """Format a match comparison log message."""
    match_indicator = "✓ MATCH" if is_match else "  COMPARE"
    return f"{match_indicator} | {app_fp} → {station} (ts={ref_ts}) | sim={similarity:.3f} | offset={offset}"

def format_summary_log(app_fp: str, app_ts: int, best_match: Optional[Tuple], best_sim: float, best_station: str, best_offset: int) -> str:
    """Format a summary log message."""
    if best_match:
        return f"✓ SUMMARY | {app_fp} (ts={app_ts}) → {best_match[1]} (ts={best_match[0]}) | sim={best_match[2]:.3f} | offset={best_match[3]}"
    else:
        return f"✗ SUMMARY | {app_fp} (ts={app_ts}) → No match | Best: {best_station} (sim={best_sim:.3f}, offset={best_offset})"

def format_error_log(error_type: str, details: str) -> str:
    """Format an error log message."""
    return f"✗ ERROR | {error_type} | {details}"

def format_info_log(info_type: str, details: str) -> str:
    """Format an info log message."""
    return f"ℹ INFO | {info_type} | {details}"

def log_report_start(app_fp: str, app_ts: int):
    """Log the start of a new fingerprint report."""
    logger.info(format_info_log("REPORT", f"Starting new fingerprint report for {app_fp} (ts={app_ts})"), extra={'fingerprint_id': f"{app_fp}_{app_ts}"})

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
        
        self.last_app_fingerprint_time = 0  # Track last downloaded AppFingerprint
        
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
                    # Handle both LiveStreamFingerprint and AppFingerprint formats
                    # Format: <HH-mm-ss>_<prefix>_<station>_<interval>s.json or <HH-mm-ss>_<prefix>_<interval>s.json
                    parts = filename.split('_')
                    if len(parts) < 3:  # Need at least HH-mm-ss, prefix, and station/interval
                        continue
                        
                    # Extract timestamp from HH-mm-ss format
                    time_str = parts[0]
                    hh, mm, ss = map(int, time_str.split('-'))
                    now = datetime.now()
                    dt = now.replace(hour=hh, minute=mm, second=ss, microsecond=0)
                    timestamp = int(dt.timestamp())
                    
                    # Extract station name
                    if 'LiveStreamFingerprint' in filename:
                        # For LiveStreamFingerprint: HH-mm-ss_LiveStreamFingerprint_station_interval.json
                        station = parts[2]
                    else:
                        # For AppFingerprint: HH-mm-ss_AppFingerprint_interval.json
                        continue  # Skip app fingerprints in reference loading
                        
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
            
            # Download any new AppFingerprints from Firebase
            self._download_app_fingerprints()
            
        except Exception as e:
            logger.error(f"Error loading reference fingerprints: {str(e)}")

    def _download_app_fingerprints(self):
        """Download new AppFingerprints from Firebase and save them locally, with detailed logging."""
        try:
            logger.info(format_info_log("SCAN", "Scanning Firebase for new AppFingerprints..."))
            blobs = self.bucket.list_blobs(prefix="fingerprints/")
            found_any = False
            device_files = {}
            
            for blob in blobs:
                if "AppFingerprint" in blob.name and blob.name.endswith(".json"):
                    found_any = True
                    try:
                        time_str = blob.name.split('/')[-1].split('_')[0]
                        hh, mm, ss = map(int, time_str.split('-'))
                        now = datetime.now()
                        dt = now.replace(hour=hh, minute=mm, second=ss, microsecond=0)
                        timestamp = int(dt.timestamp())
                        
                        if timestamp > self.last_app_fingerprint_time:
                            logger.info(format_info_log("PROCESS", f"Processing {blob.name} (ts={timestamp})"))
                            
                            data = json.loads(blob.download_as_text())
                            device_id = data.get('device_id', 'unknown')
                            
                            if device_id not in device_files:
                                device_files[device_id] = []
                            device_files[device_id].append((timestamp, blob, data))
                            
                            blob.make_public()
                            logger.info(format_info_log("PUBLIC", f"Made {blob.name} public: {blob.public_url}"))
                            
                            local_path = os.path.join(FINGERPRINT_OUTPUT_PATH, os.path.basename(blob.name))
                            os.makedirs(os.path.dirname(local_path), exist_ok=True)
                            with open(local_path, 'w') as f:
                                json.dump(data, f)
                            logger.info(format_info_log("SAVE", f"Saved to {local_path}"))
                            
                            self.last_app_fingerprint_time = max(self.last_app_fingerprint_time, timestamp)
                            
                    except Exception as e:
                        logger.error(format_error_log("PROCESS_ERROR", f"Error processing {blob.name}: {str(e)}"))
                        continue
            
            if not found_any:
                logger.warning(format_info_log("SCAN", "No new AppFingerprints found"))
                return
            
            # Process each device's newest file
            for device_id, files in device_files.items():
                files_sorted = sorted(files, key=lambda x: x[0], reverse=True)
                newest = files_sorted[0]
                timestamp, blob, data = newest
                
                if blob.name in self.processed_files:
                    continue
                    
                uploaded_fp = data.get('fingerprint')
                uploaded_ts = data.get('timestamp')
                
                debug_log_lines = []
                def debug_logger(msg):
                    debug_log_lines.append(msg)
                    logger.info(msg)
                    
                best_match, summary_line = self._find_best_match_with_log(
                    uploaded_fp, 
                    app_fp_filename=os.path.basename(blob.name),
                    app_fp_timestamp=uploaded_ts,
                    debug_logger=debug_logger
                )
                
                if best_match:
                    match_ts, match_station, similarity, offset = best_match
                    try:
                        self._log_successful_match(
                            os.path.basename(blob.name),
                            match_station,
                            similarity,
                            offset,
                            len(uploaded_fp),
                            offset,
                            datetime.now().isoformat()
                        )
                    except Exception as e:
                        logger.error(format_error_log("LOG_ERROR", f"Error logging match: {e}"))
                        
                    if uploaded_ts > 1e12:  # If in ms, convert to seconds
                        ts_sec = int(uploaded_ts // 1000)
                    else:
                        ts_sec = int(uploaded_ts)
                    time_str = datetime.fromtimestamp(ts_sec).strftime("%H-%M-%S")

                    result = {
                        "matched_at": int(time.time() * 1000),
                        "match_timestamp": match_ts,
                        "station": match_station,
                        "confidence": float(similarity),
                        "debug_log": summary_line,
                        "time_str": time_str  # For easier debugging
                    }
                    self.db.collection("results").document(time_str).set(result)
                    logger.info(format_info_log("FIRESTORE", f"Wrote match result: {match_station} (sim={similarity:.3f}) at {time_str}"))
                else:
                    result = {
                        "matched_at": int(time.time() * 1000),
                        "station": "Unknown",
                        "confidence": 0.0,
                        "debug_log": summary_line
                    }
                    self.db.collection("results").document(time_str).set(result)
                    logger.info(format_info_log("FIRESTORE", "Wrote NO MATCH result"))
                    
                self.processed_files.add(blob.name)
                
        except Exception as e:
            logger.error(format_error_log("DOWNLOAD_ERROR", f"Error processing fingerprints: {e}"))
            logger.error("Full error details:", exc_info=True)

    def _find_best_match_with_log(self, uploaded_fp: List[List[float]], app_fp_filename: str = "", app_fp_timestamp: int = 0, debug_logger=None) -> (Optional[Tuple[int, str, float, int]], str):
        """
        Find the best matching reference fingerprint using sliding window/cross-correlation.
        Returns (timestamp, station, similarity, offset) for the best match.
        Logs every comparison, best match, and warnings as requested.
        """
        try:
            # Log start of new report
            log_report_start(app_fp_filename, app_fp_timestamp)
            
            best_match = None
            best_similarity = float('-inf')
            best_offset = 0
            best_station = None
            best_ref_filename = None
            uploaded_fp = np.array(uploaded_fp)
            if len(uploaded_fp.shape) == 1:
                uploaded_fp = uploaded_fp.reshape(1, -1)
            
            # Normalize the uploaded fingerprint once
            uploaded_norms = np.linalg.norm(uploaded_fp, axis=1) + 1e-10
            uploaded_normalized = uploaded_fp / uploaded_norms[:, np.newaxis]
            
            logger.info(format_info_log("MATCHING", f"Starting match for {app_fp_filename} (ts={app_fp_timestamp})"), 
                       extra={'fingerprint_id': f"{app_fp_filename}_{app_fp_timestamp}"})
            
            for station, fingerprints in self.reference_fingerprints.items():
                for ref_ts, ref_st, ref_fp in fingerprints:
                    ref_fp = np.array(ref_fp)
                    if len(ref_fp.shape) == 1:
                        ref_fp = ref_fp.reshape(1, -1)
                    n = len(ref_fp)
                    m = len(uploaded_fp)
                    if m > n:
                        continue
                    
                    # Normalize reference fingerprint once
                    ref_norms = np.linalg.norm(ref_fp, axis=1) + 1e-10
                    ref_normalized = ref_fp / ref_norms[:, np.newaxis]
                    
                    for offset in range(n - m + 1):
                        window = ref_normalized[offset:offset + m]
                        sims = np.sum(uploaded_normalized * window, axis=1)
                        avg_sim = np.mean(sims)
                        
                        # Log comparison with better formatting
                        msg = format_match_log(
                            app_fp_filename, ref_ts, station, avg_sim, offset,
                            is_match=(avg_sim >= self.MATCH_THRESHOLD)
                        )
                        if debug_logger:
                            debug_logger(msg)
                        else:
                            logger.debug(msg)
                        
                        if avg_sim > best_similarity:
                            best_similarity = avg_sim
                            best_match = (ref_ts, station, avg_sim, offset)
                            best_offset = offset
                            best_station = station
                            best_ref_filename = f"{ref_ts}_{station}"
                        
                        if avg_sim >= self.MATCH_THRESHOLD:
                            match_msg = f"MATCH: AppFingerprint {app_fp_filename} (ts={app_fp_timestamp}) best matches Ref ts={ref_ts} (station={station}) with similarity {avg_sim:.4f} at offset {offset}"
                            if debug_logger:
                                debug_logger(match_msg)
                            else:
                                logger.info(match_msg, extra={'fingerprint_id': f"{app_fp_filename}_{app_fp_timestamp}"})
                            
            # End of all comparisons - log summary with separator
            summary = format_summary_log(
                app_fp_filename, app_fp_timestamp,
                best_match, best_similarity, best_station, best_offset
            )
            if debug_logger:
                debug_logger(summary)
            else:
                logger.info(summary, extra={'fingerprint_id': f"{app_fp_filename}_{app_fp_timestamp}"})
            
            if best_match and best_similarity >= self.MATCH_THRESHOLD:
                return best_match, summary
            else:
                warn = f"WARNING: No match found for AppFingerprint {app_fp_filename} (ts={app_fp_timestamp}). Best similarity was {best_similarity:.4f} with Ref {best_ref_filename} (station {best_station}) at offset {best_offset}"
                summary = f"SUMMARY: AppFingerprint {app_fp_filename} (ts={app_fp_timestamp}) BEST SIMILARITY: {best_similarity:.4f} with Ref {best_ref_filename} (station {best_station}) at offset {best_offset}"
                if debug_logger:
                    debug_logger(warn)
                    debug_logger(summary)
                else:
                    logger.warning(warn)
                    logger.info(summary)
                return None, summary
        except Exception as e:
            error_msg = format_error_log("MATCH_ERROR", f"Error matching {app_fp_filename}: {str(e)}")
            logger.error(error_msg, extra={'fingerprint_id': f"{app_fp_filename}_{app_fp_timestamp}"})
            return None, error_msg

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

def extract_timestamp_from_filename(filename):
    # Handle both LiveStreamFingerprint and AppFingerprint formats
    # Format: <HH-mm-ss>_<prefix>_<station>_<interval>s.json or <HH-mm-ss>_<prefix>_<interval>s.json
    try:
        time_str = filename.split('_')[0]  # Get HH-mm-ss part
        hh, mm, ss = map(int, time_str.split('-'))
        # Create a datetime for today with the extracted minutes and seconds
        now = datetime.now()
        dt = now.replace(hour=hh, minute=mm, second=ss, microsecond=0)
        return int(dt.timestamp())
    except (ValueError, IndexError):
        return None

def open_log_in_vscode():
    """Open the log file in VS Code with auto-update enabled."""
    try:
        # Get the absolute path to the log file
        log_path = os.path.abspath(LOG_FILE)
        
        # Try to open in VS Code
        if sys.platform == 'win32':
            # Windows
            subprocess.Popen(['code', '--new-window', log_path], 
                           creationflags=subprocess.CREATE_NEW_CONSOLE)
        else:
            # Linux/Mac
            subprocess.Popen(['code', '--new-window', log_path])
            
        print(f"Opening log file in VS Code: {log_path}")
        print("Tip: The log will auto-update as new entries are added")
    except Exception as e:
        print(f"Could not open log in VS Code: {e}")
        print(f"Please open {LOG_FILE} manually in your preferred text editor")

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
            # Clear log file before starting
            clear_log_file()
            
            # Open log file in VS Code
            open_log_in_vscode()
            
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