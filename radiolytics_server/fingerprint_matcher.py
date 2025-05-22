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

# --- CONFIGURATION ---
POLL_INTERVAL = 5  # seconds
MATCH_THRESHOLD = 0.75  # Increased threshold to 75% similarity for more accurate matches
BUFFER_MINUTES = 3  # Match against last 3 minutes of fingerprints
MIN_FRAMES_MATCH = 10  # Minimum number of frames that must match above threshold

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
cred = credentials.Certificate("service-account.json")
firebase_admin.initialize_app(cred, {
    'storageBucket': 'newbuckettest.firebasestorage.app'
})
bucket = storage.bucket()
db = firestore.client()

class FingerprintMatcher:
    def __init__(self):
        self.processed_files = set()
        self.reference_fingerprints = {}  # station -> list of (timestamp, station, fingerprint)
        self.MATCH_THRESHOLD = 0.75  # 75% similarity required for a match
        self.MIN_FRAMES_MATCH = 10  # Minimum frames that must match above threshold
        self.BUFFER_MINUTES = 3  # Match against last 3 minutes of fingerprints
        self.is_running = False
        self.thread = None
        
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
            
            # List all reference fingerprints
            blobs = self.bucket.list_blobs(prefix="reference_fingerprints/")
            
            for blob in blobs:
                if not blob.name.endswith('.json'):
                    continue
                    
                # Extract timestamp and station from filename
                try:
                    filename = blob.name.split('/')[-1]
                    timestamp = int(filename.split('_')[0])
                    station = filename.split('_')[1].split('.')[0]
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
            # List all incoming fingerprints
            blobs = self.bucket.list_blobs(prefix="incoming_fingerprints/")
            
            for blob in blobs:
                if blob.name in self.processed_files:
                    continue
                    
                if not blob.name.endswith('.json'):
                    continue
                    
                # Skip the latest fingerprint file as it's being actively used
                if blob.name == "incoming_fingerprints/latest.json":
                    continue
                
                try:
                    # Download and parse
                    data = json.loads(blob.download_as_text())
                    uploaded_fp = data.get('fingerprint')
                    uploaded_ts = data.get('timestamp')
                    device_id = data.get('device_id')
                    
                    if not all([uploaded_fp, uploaded_ts, device_id]):
                        logger.warning(f"Missing data in {blob.name}")
                        continue
                    
                    # Find best match
                    best_match = self._find_best_match(uploaded_fp)
                    
                    if best_match:
                        match_ts, match_station, similarity = best_match
                        # Write result to Firestore
                        result = {
                            "matched_at": int(time.time() * 1000),
                            "match_timestamp": match_ts,
                            "station": match_station,
                            "confidence": float(similarity)
                        }
                        self.db.collection("results").document(str(uploaded_ts)).set(result)
                        logger.info(f"Matched {blob.name} to {match_station} with confidence {similarity:.2f}")
                    else:
                        # Write "no match" result
                        result = {
                            "matched_at": int(time.time() * 1000),
                            "station": "Unknown",
                            "confidence": 0.0
                        }
                        self.db.collection("results").document(str(uploaded_ts)).set(result)
                        logger.info(f"No match found for {blob.name}")
                    
                    # Mark as processed
                    self.processed_files.add(blob.name)
                    
                    # Also save locally
                    local_dir = os.path.join('Fingerprints', 'local_incoming_fingerprints')
                    os.makedirs(local_dir, exist_ok=True)
                    local_path = os.path.join(local_dir, os.path.basename(blob.name))
                    with open(local_path, 'w') as f:
                        f.write(blob.download_as_text())
                    logger.info(f"Saved incoming fingerprint locally at {local_path}")
                    
                    # Instead of deleting, move to processed_fingerprints directory with public read access
                    try:
                        # Create a new blob in processed_fingerprints
                        new_blob_name = f"processed_fingerprints/{os.path.basename(blob.name)}"
                        new_blob = self.bucket.blob(new_blob_name)
                        
                        # Set content type and public read access
                        new_blob.content_type = 'application/json'
                        new_blob.content_disposition = 'attachment; filename="' + os.path.basename(blob.name) + '"'
                        
                        # Copy the content
                        new_blob.upload_from_string(
                            blob.download_as_text(),
                            content_type='application/json'
                        )
                        
                        # Make the blob publicly readable
                        new_blob.make_public()
                        
                        # Get the public URL
                        public_url = new_blob.public_url
                        logger.info(f"Moved {blob.name} to {new_blob_name} - Public URL: {public_url}")
                        
                        # Delete the original blob
                        blob.delete()
                        
                    except Exception as e:
                        logger.error(f"Error moving fingerprint file {blob.name}: {str(e)}")
                        # Don't delete the original if move fails
                        continue
                    
                except Exception as e:
                    logger.error(f"Error processing {blob.name}: {str(e)}")
                    # Log the full error details
                    logger.error(f"Full error details for {blob.name}:", exc_info=True)
                    continue
                    
        except Exception as e:
            logger.error(f"Error processing incoming fingerprints: {str(e)}")
            logger.error("Full error details:", exc_info=True)

    def _find_best_match(self, uploaded_fp):
        """Find the best matching reference fingerprint with improved logging"""
        try:
            best_match = None
            best_similarity = 0.0
            
            # Convert uploaded fingerprint to numpy array
            uploaded_fp = np.array(uploaded_fp)
            
            # Ensure uploaded fingerprint is 2D array
            if len(uploaded_fp.shape) == 1:
                uploaded_fp = uploaded_fp.reshape(1, -1)
            
            logger.info(f"Finding match for uploaded fingerprint with shape: {uploaded_fp.shape}")
            
            for station, fingerprints in self.reference_fingerprints.items():
                logger.debug(f"Checking {len(fingerprints)} fingerprints for station {station}")
                
                for ref_ts, ref_st, ref_fp in fingerprints:
                    try:
                        # Convert reference fingerprint to numpy array
                        ref_fp = np.array(ref_fp)
                        
                        # Ensure reference fingerprint is 2D array
                        if len(ref_fp.shape) == 1:
                            ref_fp = ref_fp.reshape(1, -1)
                        
                        # Calculate similarity
                        similarity = self._cosine_similarity(uploaded_fp, ref_fp)
                        
                        if similarity > best_similarity:
                            best_similarity = similarity
                            best_match = (ref_ts, station, similarity)
                            logger.debug(f"New best match: {station} at {ref_ts} with confidence {similarity:.3f}")
                            
                    except Exception as e:
                        logger.error(f"Error processing reference fingerprint for {station}: {str(e)}")
                        continue
            
            if best_match:
                logger.info(f"Best match found: {best_match[1]} at {best_match[0]} with confidence {best_match[2]:.3f}")
            else:
                logger.info("No match found above threshold")
                
            return best_match
            
        except Exception as e:
            logger.error(f"Error in find_best_match: {str(e)}")
            return None

    def _cosine_similarity(self, fp1, fp2):
        """Calculate cosine similarity between two fingerprints with improved validation"""
        try:
            # Ensure both fingerprints are 2D arrays
            fp1 = np.array(fp1)
            fp2 = np.array(fp2)
            
            if len(fp1.shape) == 1:
                fp1 = fp1.reshape(1, -1)
            if len(fp2.shape) == 1:
                fp2 = fp2.reshape(1, -1)
            
            # Log fingerprint shapes for debugging
            logger.debug(f"Fingerprint shapes - fp1: {fp1.shape}, fp2: {fp2.shape}")
            
            # Validate fingerprint lengths
            if fp1.shape[1] != fp2.shape[1]:
                logger.warning(f"Fingerprint dimension mismatch: {fp1.shape[1]} vs {fp2.shape[1]}")
                return 0.0
                
            # Normalize each frame
            fp1_norm = fp1 / (np.linalg.norm(fp1, axis=1, keepdims=True) + 1e-10)
            fp2_norm = fp2 / (np.linalg.norm(fp2, axis=1, keepdims=True) + 1e-10)
            
            # Calculate similarity for each frame
            similarities = []
            for i in range(min(len(fp1), len(fp2))):
                sim = np.dot(fp1_norm[i], fp2_norm[i])
                similarities.append(sim)
            
            # Count frames that exceed threshold
            high_similarity_frames = sum(1 for s in similarities if s >= self.MATCH_THRESHOLD)
            avg_similarity = np.mean(similarities) if similarities else 0.0
            
            # Log detailed matching info
            logger.debug(f"Frame similarities - avg: {avg_similarity:.3f}, "
                        f"high similarity frames: {high_similarity_frames}/{len(similarities)}")
            
            # Only return high similarity if enough frames match
            if high_similarity_frames >= self.MIN_FRAMES_MATCH:
                return avg_similarity
            else:
                logger.debug(f"Insufficient high-similarity frames: {high_similarity_frames} < {self.MIN_FRAMES_MATCH}")
                return 0.0
            
        except Exception as e:
            logger.error(f"Error calculating similarity: {str(e)}")
            return 0.0

def main():
    try:
        matcher = FingerprintMatcher()
        matcher.start()
        
        # Keep the main thread alive
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            logger.info("Shutting down...")
        finally:
            matcher.stop()
            
    except Exception as e:
        logger.error(f"Main error: {str(e)}")

if __name__ == "__main__":
    main() 