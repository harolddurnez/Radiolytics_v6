import os
import time
import json
import numpy as np
import requests
from io import BytesIO
import firebase_admin
from firebase_admin import credentials, storage
import logging
from datetime import datetime
import threading
from collections import deque
from dotenv import load_dotenv
import importlib
import wave
import subprocess

# --- CONFIGURATION ---
RECORDING_INTERVAL = 7 # seconds
BUFFER_MINUTES = 3      # Keep 3 minutes of fingerprints
SAMPLE_RATE = 8000      # Hz
CHANNELS = 1           # mono
CHUNK_SIZE = 512       # samples
OVERLAP = 0.5          # 50% overlap

# --- LOGGING SETUP ---
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
LOGGING_DIR = os.path.join(PROJECT_ROOT, 'LOGGING')
REFERENCE_FP_DIR = os.path.join(LOGGING_DIR, 'fingerprints')
REFERENCE_RECORDER_LOG_PATH = os.path.join(LOGGING_DIR, 'reference_recorder.log')

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(REFERENCE_RECORDER_LOG_PATH),
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

# Update all local fingerprint file output to use FINGERPRINT_OUTPUT_PATH from .env
FINGERPRINT_OUTPUT_PATH = REFERENCE_FP_DIR
logger.info(f"Using fingerprint output path: {FINGERPRINT_OUTPUT_PATH}")

class RadioStreamRecorder:
    def __init__(self, stream_url, station_name, headers=None):
        self.stream_url = stream_url
        self.station_name = station_name
        self.headers = headers or {}
        self.is_running = False
        self.thread = None
        self.fingerprints = deque()  # (timestamp, fingerprint)
        
        # Audio parameters
        self.SAMPLE_RATE = SAMPLE_RATE
        self.CHANNELS = CHANNELS
        self.CHUNK_SIZE = CHUNK_SIZE
        self.OVERLAP = OVERLAP
        
        # Calculate buffer size for 10 seconds of audio
        self.buffer_size = int(SAMPLE_RATE * RECORDING_INTERVAL)
        
        # Calculate number of frames per recording (matching Android app)
        self.frames_per_recording = int(RECORDING_INTERVAL * 1000 / 50)  # 50ms per frame
        
        logger.info(f"Initialized recorder for {station_name} with {self.frames_per_recording} frames per recording")

    def start(self):
        if self.is_running:
            return
            
        self.is_running = True
        self.thread = threading.Thread(target=self._record_loop, daemon=True)
        self.thread.start()
        logger.info(f"Started recording {self.station_name}")

    def stop(self):
        self.is_running = False
        if self.thread:
            self.thread.join()
        logger.info(f"Stopped recording {self.station_name}")

    def _record_loop(self):
        while self.is_running:
            try:
                # Record 10 seconds of audio
                audio_data = self._capture_audio()
                if audio_data is not None:
                    # Generate fingerprint
                    fingerprint = self._generate_fingerprint(audio_data)
                    if fingerprint is not None:
                        timestamp = int(time.time())
                        self._save_fingerprint(fingerprint, timestamp)
                        
                        # Add to local buffer
                        self.fingerprints.append((timestamp, fingerprint))
                        
                        # Remove old fingerprints
                        cutoff = timestamp - (BUFFER_MINUTES * 60)
                        while self.fingerprints and self.fingerprints[0][0] < cutoff:
                            self.fingerprints.popleft()
                
                # Wait until next recording interval
                time.sleep(RECORDING_INTERVAL)
                
            except Exception as e:
                logger.error(f"Error in record loop for {self.station_name}: {str(e)}")
                time.sleep(5)  # Wait before retrying

    def _capture_audio(self):
        """Capture 7 seconds of audio from the stream using ffmpeg subprocess for robust streaming."""
        try:
            ffmpeg_cmd = [
                'ffmpeg',
                '-loglevel', 'error',
                '-i', self.stream_url,
                '-t', str(RECORDING_INTERVAL),
                '-ar', str(self.SAMPLE_RATE),
                '-ac', '1',
                '-f', 's16le',
                '-vn',
                'pipe:1'
            ]
            logger.info(f"Launching ffmpeg: {' '.join(ffmpeg_cmd)}")
            proc = subprocess.Popen(ffmpeg_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            num_samples = self.SAMPLE_RATE * RECORDING_INTERVAL
            num_bytes = num_samples * 2  # 16-bit PCM
            raw_audio = b''
            while len(raw_audio) < num_bytes:
                chunk = proc.stdout.read(num_bytes - len(raw_audio))
                if not chunk:
                    break
                raw_audio += chunk
            proc.stdout.close()
            proc.wait()
            if len(raw_audio) < num_bytes:
                logger.warning(f"ffmpeg: Only got {len(raw_audio)} bytes, expected {num_bytes}. Padding with zeros.")
                raw_audio += b'\x00' * (num_bytes - len(raw_audio))
            elif len(raw_audio) > num_bytes:
                raw_audio = raw_audio[:num_bytes]
            audio_data = np.frombuffer(raw_audio, dtype=np.int16).astype(np.float32) / 32768.0
            logger.info(f"ffmpeg: Read {len(audio_data)} samples from stream.")
            self._last_audio_data = audio_data
            return audio_data
        except Exception as e:
            logger.error(f"Error capturing audio with ffmpeg: {str(e)}")
            self._last_audio_data = None
            return None

    def _generate_fingerprint(self, audio_data):
        """Generate fingerprint from audio data"""
        try:
            # Convert audio data to numpy array
            audio_array = np.frombuffer(audio_data, dtype=np.float32)
            
            # Split audio into chunks of CHUNK_SIZE samples
            num_chunks = len(audio_array) // self.CHUNK_SIZE
            chunks = [audio_array[i:i + self.CHUNK_SIZE] for i in range(0, num_chunks * self.CHUNK_SIZE, self.CHUNK_SIZE)]
            
            # Generate fingerprint for each chunk
            fingerprint = []
            for chunk in chunks:
                # Calculate RMS energy
                rms = np.sqrt(np.mean(np.square(chunk)))
                
                # Calculate spectral centroid (normalize to 0-1)
                spectrum = np.abs(np.fft.rfft(chunk))
                freqs = np.fft.rfftfreq(len(chunk), 1.0/self.SAMPLE_RATE)
                if np.sum(spectrum) == 0:
                    centroid = 0
                else:
                    centroid = np.sum(freqs * spectrum) / np.sum(spectrum)
                centroid = centroid / (self.SAMPLE_RATE / 2)  # Normalize to 0-1
                
                # Calculate energy as mean absolute value (time-domain)
                energy = np.mean(np.abs(chunk))
                
                # Calculate decibel (dB) value for the chunk
                db = 20 * np.log10(rms + 1e-10)  # Add epsilon to avoid log(0)
                
                # Create 4D vector (RMS, spectral centroid, energy, dB)
                frame = [float(rms), float(centroid), float(energy), float(db)]
                fingerprint.append(frame)
                logger.debug(f"Frame dB: {db:.2f}")
            
            # Ensure we have the expected number of frames
            if len(fingerprint) > self.frames_per_recording:
                # Take evenly spaced frames to match expected count
                indices = np.linspace(0, len(fingerprint)-1, self.frames_per_recording, dtype=int)
                fingerprint = [fingerprint[i] for i in indices]
            elif len(fingerprint) < self.frames_per_recording:
                # Pad with zeros to match expected count
                padding = [[0.0, 0.0, 0.0, 0.0]] * (self.frames_per_recording - len(fingerprint))
                fingerprint.extend(padding)

            # --- DEBUG LOGGING: Feature statistics ---
            fp_np = np.array(fingerprint)
            if fp_np.shape[0] > 0:
                stats = {}
                for i, name in enumerate(["RMS", "Centroid", "Energy", "dB"]):
                    stats[name] = {
                        "mean": float(np.mean(fp_np[:, i])),
                        "min": float(np.min(fp_np[:, i])),
                        "max": float(np.max(fp_np[:, i]))
                    }
                logger.info(f"Feature stats for {self.station_name} fingerprint: {json.dumps(stats)}")
                # Count valid frames (not all zero, dB > -155)
                valid_mask = np.logical_and(np.any(fp_np[:, :3] != 0, axis=1), fp_np[:, 3] > -155)
                num_valid = int(np.sum(valid_mask))
                logger.info(f"Valid frames for {self.station_name} fingerprint: {num_valid} / {len(fingerprint)}")
                # Log large continuous silent regions (optional)
                silent_mask = ~valid_mask
                max_silent = 0
                current = 0
                for v in silent_mask:
                    if v:
                        current += 1
                        if current > max_silent:
                            max_silent = current
                    else:
                        current = 0
                if max_silent > 10:
                    logger.warning(f"LARGE SILENT REGION: {max_silent} consecutive silent frames in {self.station_name} fingerprint.")
                if num_valid >= 0.75 * len(fingerprint):
                    logger.info(f"HEALTHY: {self.station_name} reference extraction: {num_valid} valid frames out of {len(fingerprint)}")
                else:
                    logger.warning(f"UNHEALTHY: {self.station_name} reference extraction: only {num_valid} valid frames out of {len(fingerprint)}")
            # --- END DEBUG LOGGING ---

            return fingerprint
            
        except Exception as e:
            logger.error(f"Error generating fingerprint: {str(e)}")
            return None

    def _save_fingerprint(self, fingerprint, timestamp):
        """Save fingerprint to Firebase Storage and local directory, and save audio as WAV"""
        try:
            if fingerprint is None:
                return
            # Create JSON with metadata
            data = {
                "timestamp": timestamp,
                "station": self.station_name,
                "fingerprint": fingerprint,
                "sample_rate": self.SAMPLE_RATE,
                "channels": self.CHANNELS
            }
            # Convert to JSON
            json_data = json.dumps(data)
            # Format timestamp as HH-mm-ss
            dt = datetime.fromtimestamp(timestamp)
            time_str = dt.strftime("%H-%M-%S")
            filename = f"{time_str}_LiveStreamFingerprint_{self.station_name}_{RECORDING_INTERVAL}s.json"
            # Upload to Firebase Storage
            blob = bucket.blob(f"fingerprints/{filename}")
            blob.upload_from_string(json_data, content_type='application/json')
            blob.content_disposition = f'attachment; filename="{filename}"'
            blob.patch()  # Save the content_disposition
            blob.make_public()  # Make the blob publicly readable
            logger.info(f"Saved fingerprint for {self.station_name} at {timestamp} - Public URL: {blob.public_url}")
            # Also save locally
            local_dir = FINGERPRINT_OUTPUT_PATH
            os.makedirs(local_dir, exist_ok=True)
            local_path = os.path.join(local_dir, filename)
            with open(local_path, 'w') as f:
                json.dump({"fingerprint": fingerprint, "timestamp": timestamp, "station": self.station_name}, f)
            logger.info(f"Saved reference fingerprint locally at {local_path}")
            # --- Save WAV file for inspection ---
            try:
                # Retrieve the last captured audio from self._last_audio_data (set in _capture_audio)
                audio_data = getattr(self, '_last_audio_data', None)
                if audio_data is not None:
                    wav_filename = f"{time_str}_LiveStreamAudio_{self.station_name}_{RECORDING_INTERVAL}s.wav"
                    wav_path = os.path.join(local_dir, wav_filename)
                    logger.info(f"Writing WAV audio for {self.station_name} to {wav_path}")
                    with wave.open(wav_path, 'wb') as wf:
                        wf.setnchannels(1)
                        wf.setsampwidth(2)  # 16-bit PCM
                        wf.setframerate(self.SAMPLE_RATE)
                        audio_int16 = (audio_data * 32767.0).clip(-32768, 32767).astype(np.int16)
                        wf.writeframes(audio_int16.tobytes())
                    logger.info(f"Saved WAV audio for inspection at {wav_path}")
                else:
                    logger.warning(f"No audio data available to write WAV for {self.station_name} at {time_str}")
            except Exception as e:
                logger.error(f"Error writing WAV file for {self.station_name}: {str(e)}")
            # --- End WAV save ---
            # Clean up old fingerprints
            self._cleanup_old_fingerprints()
        except Exception as e:
            logger.error(f"Error saving fingerprint: {str(e)}")

    def _cleanup_old_fingerprints(self):
        """Clean up fingerprints older than BUFFER_MINUTES"""
        try:
            # List all fingerprints in the unified folder
            blobs = bucket.list_blobs(prefix="fingerprints/")
            current_time = time.time()
            
            for blob in blobs:
                try:
                    # Extract timestamp from filename
                    filename = blob.name.split('/')[-1]
                    # Extract timestamp from <HH-mm-ss>_LiveStreamFingerprint_<station>_<interval>s.json
                    time_str = filename.split('_')[0]  # Get mm-ss part
                    hh, mm, ss = map(int, time_str.split('-'))
                    # Create a datetime for today with the extracted minutes and seconds
                    now = datetime.now()
                    dt = now.replace(hour=hh, minute=mm, second=ss, microsecond=0)
                    timestamp = int(dt.timestamp())
                    
                    # Delete if older than buffer time
                    if current_time - timestamp > BUFFER_MINUTES * 60:
                        blob.delete()
                        logger.info(f"Deleted old fingerprint: {blob.name}")
                        
                except Exception as e:
                    logger.error(f"Error processing blob {blob.name}: {str(e)}")
                    continue
                
        except Exception as e:
            logger.error(f"Error cleaning up old fingerprints: {str(e)}")

    def get_fingerprints(self):
        """Return a copy of the current fingerprints buffer"""
        return list(self.fingerprints)

def load_station_config():
    """Load radio station configuration from config.json"""
    try:
        with open('config.json', 'r') as f:
            config = json.load(f)
        return config.get('stations', {})
    except Exception as e:
        logger.error(f"Error loading config: {str(e)}")
        return {}

def main():
    try:
        # Load station configuration
        stations = load_station_config()
        if not stations:
            logger.error("No stations configured in config.json")
            return

        # Create recorders for each station
        recorders = {}
        for name, url in stations.items():
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36"
            }
            recorders[name] = RadioStreamRecorder(url, name, headers)
            recorders[name].start()
            logger.info(f"Started recorder for {name}")

        # Keep the main thread alive
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            logger.info("Shutting down...")
        finally:
            # Stop all recorders
            for recorder in recorders.values():
                recorder.stop()

    except Exception as e:
        logger.error(f"Main error: {str(e)}")

if __name__ == "__main__":
    main() 