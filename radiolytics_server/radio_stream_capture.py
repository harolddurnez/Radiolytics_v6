import pyaudio
import numpy as np
import threading
import time
import requests
from io import BytesIO
import logging
from pydub import AudioSegment
import ffmpeg
import tempfile
import os
from collections import deque
import json
import wave

class RadioStreamCapture:
    def __init__(self, stream_url, station_name, headers=None, buffer_seconds=60):
        self.stream_url = stream_url
        self.station_name = station_name
        self.headers = headers or {}
        self.buffer_seconds = buffer_seconds
        self.is_running = False
        self.thread = None
        self.fingerprints = deque()  # (timestamp, fingerprint)
        self.audio = pyaudio.PyAudio()
        
        # Audio parameters (matching Android app)
        self.SAMPLE_RATE = 8000
        self.FRAME_SIZE = 512
        self.FRAME_OVERLAP = 256
        self.CHANNELS = 1
        self.CHUNK_SIZE = self.FRAME_SIZE
        
        # FFT setup
        self.window = np.hanning(self.FRAME_SIZE)
        
        # Create a temporary directory for audio processing
        self.temp_dir = tempfile.mkdtemp()
        
        logging.basicConfig(level=logging.INFO)
        self.logger = logging.getLogger(f"RadioStreamCapture-{station_name}")

    def start(self):
        if self.is_running:
            return
            
        self.is_running = True
        self.thread = threading.Thread(target=self._capture_loop, daemon=True)
        self.thread.start()
        self.logger.info(f"Started capturing stream for {self.station_name}")

    def stop(self):
        self.is_running = False
        if self.thread:
            self.thread.join()
        self.audio.terminate()
        # Clean up temporary files
        try:
            import shutil
            shutil.rmtree(self.temp_dir)
        except Exception as e:
            self.logger.error(f"Error cleaning up temp directory: {str(e)}")
        self.logger.info(f"Stopped capturing stream for {self.station_name}")

    def _convert_aac_to_wav(self, aac_data):
        """Convert AAC data to WAV format using ffmpeg"""
        try:
            # Create temporary files
            aac_file = os.path.join(self.temp_dir, f"temp_{int(time.time())}.aac")
            wav_file = os.path.join(self.temp_dir, f"temp_{int(time.time())}.wav")
            
            # Write AAC data to file
            with open(aac_file, 'wb') as f:
                f.write(aac_data)
            
            # Convert to WAV using ffmpeg
            stream = ffmpeg.input(aac_file)
            stream = ffmpeg.output(stream, wav_file, acodec='pcm_s16le', ac=1, ar=self.SAMPLE_RATE)
            ffmpeg.run(stream, capture_stdout=True, capture_stderr=True, overwrite_output=True)
            
            # Read WAV data
            with open(wav_file, 'rb') as f:
                wav_data = f.read()
            
            # Clean up temporary files
            os.remove(aac_file)
            os.remove(wav_file)
            
            return wav_data
            
        except Exception as e:
            self.logger.error(f"Error converting AAC to WAV: {str(e)}")
            return None

    def _capture_loop(self):
        target_samples = self.SAMPLE_RATE * 7  # 7 seconds, or use self.buffer_seconds if variable
        while self.is_running:
            try:
                response = requests.get(self.stream_url, stream=True, headers=self.headers)
                if response.status_code != 200:
                    self.logger.error(f"Failed to connect to stream: {response.status_code}")
                    time.sleep(5)
                    continue

                buffer = BytesIO()
                total_samples = 0
                aac_data_accum = bytearray()
                start_time = time.time()
                last_log_time = start_time
                for chunk in response.iter_content(chunk_size=self.CHUNK_SIZE):
                    now = time.time()
                    if not self.is_running:
                        self.logger.warning(f"[{self.station_name}] Capture loop stopped externally.")
                        break
                    if not chunk:
                        self.logger.warning(f"[{self.station_name}] Empty chunk received at {now - start_time:.2f}s.")
                        continue
                    buffer.write(chunk)
                    aac_data_accum.extend(chunk)
                    # Try to estimate how many PCM samples we have so far
                    wav_data = self._convert_aac_to_wav(aac_data_accum)
                    if wav_data:
                        audio_data = np.frombuffer(wav_data[44:], dtype=np.int16).astype(np.float32) / 32768.0
                        total_samples = len(audio_data)
                        if now - last_log_time > 1 or total_samples >= target_samples:
                            self.logger.info(f"[{self.station_name}] Buffer: {total_samples} samples, {len(aac_data_accum)} bytes AAC, {now - start_time:.2f}s elapsed.")
                            last_log_time = now
                        if total_samples >= target_samples:
                            break
                    else:
                        self.logger.warning(f"[{self.station_name}] ffmpeg decode failed at {now - start_time:.2f}s, {len(aac_data_accum)} bytes AAC.")
                    if now - start_time > 30:  # Increased timeout to 30s
                        self.logger.warning(f"[{self.station_name}] Timeout: could not accumulate enough samples after 30s.")
                        break
                # Final conversion and processing
                wav_data = self._convert_aac_to_wav(aac_data_accum)
                if wav_data:
                    audio_data = np.frombuffer(wav_data[44:], dtype=np.int16).astype(np.float32) / 32768.0
                    if len(audio_data) < target_samples:
                        self.logger.warning(f"[{self.station_name}] Stream ended early: only {len(audio_data)} samples, padding to {target_samples}.")
                        audio_data = np.pad(audio_data, (0, target_samples - len(audio_data)))
                    elif len(audio_data) > target_samples:
                        audio_data = audio_data[:target_samples]
                    frames = self._process_audio_chunk(audio_data)
                    if frames:
                        # Log number of valid frames
                        fp_np = np.array(frames)
                        valid_mask = np.logical_and(np.any(fp_np[:, :3] != 0, axis=1), fp_np[:, 3] > -150)
                        num_valid = int(np.sum(valid_mask))
                        self.logger.info(f"[{self.station_name}] SAVED: {num_valid} valid frames out of {len(frames)}")
                        timestamp = int(time.time() * 1000)
                        self._add_fingerprint(frames, timestamp)
                        # --- Save WAV file for inspection ---
                        project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
                        out_path = os.path.join(project_root, 'LOGGING', 'fingerprints')
                        os.makedirs(out_path, exist_ok=True)
                        dt_str = time.strftime('%H-%M-%S')
                        wav_filename = f"{dt_str}_LiveStreamAudio_{self.station_name}_7s.wav"
                        wav_path = os.path.join(out_path, wav_filename)
                        with wave.open(wav_path, 'wb') as wf:
                            wf.setnchannels(1)
                            wf.setsampwidth(2)  # 16-bit PCM
                            wf.setframerate(self.SAMPLE_RATE)
                            # Convert float32 [-1,1] to int16
                            audio_int16 = (audio_data * 32767.0).clip(-32768, 32767).astype(np.int16)
                            wf.writeframes(audio_int16.tobytes())
                        self.logger.info(f"[{self.station_name}] Saved WAV audio for inspection at {wav_path}")
                        # --- End WAV save ---
                else:
                    self.logger.error(f"[{self.station_name}] Could not convert accumulated AAC to WAV.")
                # Wait before next reference
                time.sleep(self.buffer_seconds)
            except Exception as e:
                self.logger.error(f"Error in capture loop: {str(e)}")
                time.sleep(5)  # Wait before retrying

    def _process_audio_chunk(self, audio_data):
        # Split audio into overlapping frames and extract 4D features per frame
        frames = []
        step = self.FRAME_SIZE - self.FRAME_OVERLAP
        for start in range(0, len(audio_data) - self.FRAME_SIZE + 1, step):
            frame = audio_data[start:start+self.FRAME_SIZE]
            if len(frame) < self.FRAME_SIZE:
                continue
            # Windowing
            float_frame = frame * self.window
            # RMS
            rms = np.sqrt(np.mean(float_frame ** 2)).astype(np.float32)
            # Energy
            energy = np.mean(float_frame ** 2).astype(np.float32)
            # FFT for centroid
            fft = np.fft.rfft(float_frame)
            magnitudes = np.abs(fft)
            freqs = np.fft.rfftfreq(self.FRAME_SIZE, 1.0 / self.SAMPLE_RATE)
            mag_sum = np.sum(magnitudes) if np.sum(magnitudes) > 0 else 1.0
            centroid = (np.sum(freqs * magnitudes) / mag_sum).astype(np.float32)
            norm_centroid = centroid / (self.SAMPLE_RATE / 2.0)
            # dB
            db = 20 * np.log10(rms + 1e-10)
            # Match app normalization
            norm_rms = rms
            norm_energy = energy / self.FRAME_SIZE
            norm_db = db.astype(np.float32)
            frames.append([float(norm_rms), float(norm_centroid), float(norm_energy), float(norm_db)])
        return frames

    def _add_fingerprint(self, frames, timestamp):
        self.fingerprints.append((timestamp, frames))
        # Remove old fingerprints
        cutoff = int(time.time() * 1000) - self.buffer_seconds * 1000
        while self.fingerprints and self.fingerprints[0][0] < cutoff:
            self.fingerprints.popleft()
        # Save to JSON (match app format)
        project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
        out_path = os.path.join(project_root, 'LOGGING', 'fingerprints')
        os.makedirs(out_path, exist_ok=True)
        filename = f"{time.strftime('%H-%M-%S')}_LiveStreamFingerprint_{self.station_name}_7s.json"
        with open(os.path.join(out_path, filename), 'w', encoding='utf-8') as f:
            json.dump({
                "fingerprint": frames,
                "timestamp": timestamp,
                "station": self.station_name
            }, f)
        self.logger.info(f"Saved reference fingerprint locally at {os.path.join(out_path, filename)}")

    def get_fingerprints(self):
        """Return a copy of the current fingerprints buffer"""
        return list(self.fingerprints)

    def get_station_name(self):
        return self.station_name 