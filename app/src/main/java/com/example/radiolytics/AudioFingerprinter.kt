package com.example.radiolytics

import android.media.AudioFormat
import android.media.AudioRecord
import android.media.MediaRecorder
import org.jtransforms.fft.DoubleFFT_1D
import kotlin.math.pow
import kotlin.math.sqrt
import org.json.JSONArray
import org.json.JSONObject

class AudioFingerprinter {
    companion object {
        private const val SAMPLE_RATE = 8000 // As per original prompt
        private const val CHANNEL_CONFIG = AudioFormat.CHANNEL_IN_MONO
        private const val AUDIO_FORMAT = AudioFormat.ENCODING_PCM_16BIT
        private const val FRAME_SIZE = 512
        private const val FRAME_OVERLAP = 256 // 50% overlap
    }

    private var audioRecord: AudioRecord? = null
    private var isRecording = false
    private val buffer = ShortArray(FRAME_SIZE)
    private val fingerprint = mutableListOf<FloatArray>()
    private var latestDb: Double = 0.0
    private var prevFrame = ShortArray(FRAME_OVERLAP)

    fun startRecording() {
        if (isRecording) return
        val minBufferSize = AudioRecord.getMinBufferSize(SAMPLE_RATE, CHANNEL_CONFIG, AUDIO_FORMAT)
        audioRecord = AudioRecord(
            MediaRecorder.AudioSource.MIC,
            SAMPLE_RATE,
            CHANNEL_CONFIG,
            AUDIO_FORMAT,
            minBufferSize
        )
        audioRecord?.startRecording()
        isRecording = true
        fingerprint.clear()
        prevFrame.fill(0)
    }

    fun stopRecording(): ByteArray {
        isRecording = false
        audioRecord?.stop()
        audioRecord?.release()
        audioRecord = null
        // Convert fingerprint to JSON
        val jsonArray = JSONArray()
        for (frame in fingerprint) {
            val frameArray = JSONArray()
            frame.forEach { frameArray.put(it) }
            jsonArray.put(frameArray)
        }
        return jsonArray.toString().toByteArray()
    }

    fun getLatestDb(): Double = latestDb

    fun processAudioChunk(): Boolean {
        if (!isRecording) return false
        val readSize = audioRecord?.read(buffer, 0, FRAME_SIZE) ?: 0
        if (readSize < FRAME_SIZE) return false
        // Combine with previous frame for overlap
        val fullFrame = ShortArray(FRAME_SIZE)
        for (i in 0 until FRAME_OVERLAP) {
            fullFrame[i] = prevFrame[i]
        }
        for (i in 0 until FRAME_SIZE - FRAME_OVERLAP) {
            fullFrame[i + FRAME_OVERLAP] = buffer[i]
        }
        // Save last half for next overlap
        for (i in 0 until FRAME_OVERLAP) {
            prevFrame[i] = buffer[FRAME_SIZE - FRAME_OVERLAP + i]
        }
        // Convert to float
        val floatFrame = FloatArray(FRAME_SIZE) { fullFrame[it].toFloat() / 32768f }
        // RMS
        val rms = kotlin.math.sqrt(floatFrame.map { it * it }.average()).toFloat()
        // Energy
        val energy = floatFrame.map { it * it }.sum().toFloat()
        // Spectral centroid
        val fft = DoubleArray(FRAME_SIZE)
        for (i in floatFrame.indices) fft[i] = floatFrame[i].toDouble()
        val fftObj = org.jtransforms.fft.DoubleFFT_1D(FRAME_SIZE.toLong())
        fftObj.realForward(fft)
        val magnitudes = DoubleArray(FRAME_SIZE / 2)
        for (i in magnitudes.indices) {
            val real = fft[2 * i]
            val imag = fft[2 * i + 1]
            magnitudes[i] = kotlin.math.sqrt(real * real + imag * imag)
        }
        val freqs = DoubleArray(FRAME_SIZE / 2) { it * SAMPLE_RATE.toDouble() / FRAME_SIZE }
        val magSum = magnitudes.sum().takeIf { it > 0 } ?: 1.0
        val centroid = (freqs.zip(magnitudes).sumOf { it.first * it.second } / magSum).toFloat()
        // Normalize features
        val normRms = rms // Already in [0,1] for audio
        val normCentroid = centroid / (SAMPLE_RATE / 2f) // Nyquist
        val normEnergy = energy / FRAME_SIZE // Normalize by frame size
        fingerprint.add(floatArrayOf(normRms, normCentroid, normEnergy))
        // dB for monitoring
        latestDb = 20 * kotlin.math.log10(rms + 1e-10)
        return true
    }

    fun getFingerprint(): ByteArray = stopRecording()
} 