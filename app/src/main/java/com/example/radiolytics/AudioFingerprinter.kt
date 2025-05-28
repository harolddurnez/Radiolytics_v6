package com.example.radiolytics

import android.media.AudioFormat
import android.media.AudioRecord
import android.media.MediaRecorder
import org.jtransforms.fft.DoubleFFT_1D
import kotlin.math.pow
import kotlin.math.sqrt
import org.json.JSONArray
import org.json.JSONObject
import kotlin.math.ln
import be.tarsos.dsp.AudioEvent
import be.tarsos.dsp.mfcc.MFCC
import be.tarsos.dsp.io.TarsosDSPAudioFormat

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
        val energy = floatFrame.map { it * it }.average().toFloat() // mean(x^2)
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
        
        // Calculate dB level (same as Python backend)
        val db = 20 * kotlin.math.log10(rms + 1e-10)
        latestDb = db // Store for monitoring
        
        // Normalize features to match Python backend
        val normRms = rms // Already in [0,1] for    audio
        val normCentroid = centroid / (SAMPLE_RATE / 2f) // Normalize by Nyquist frequency
        val normEnergy = energy / FRAME_SIZE // Normalize by frame size
        val normDb = db.toFloat() // Keep dB as is, matching Python backend
        
        // --- MFCC Extraction ---
        val mfccs = extractMfccForFrame(floatFrame)
        // --- End MFCC Extraction ---

        // Add 4D + 13D vector to fingerprint
        val featureVec = FloatArray(4 + mfccs.size)
        featureVec[0] = normRms
        featureVec[1] = normCentroid
        featureVec[2] = normEnergy
        featureVec[3] = normDb
        for (i in mfccs.indices) {
            featureVec[4 + i] = mfccs[i]
        }
        fingerprint.add(featureVec)
        return true
    }

    fun getFingerprint(): ByteArray = stopRecording()

    // --- MFCC Helper Functions ---
    private fun hzToMel(hz: Double): Double = 2595 * kotlin.math.log10(1 + hz / 700.0)
    private fun melToHz(mel: Double): Double = 700 * ((10.0).pow(mel / 2595) - 1)
    private fun createMelFilterbank(nMels: Int, nFft: Int, sampleRate: Int): Array<DoubleArray> {
        val fMin = 0.0
        val fMax = sampleRate / 2.0
        val melMin = hzToMel(fMin)
        val melMax = hzToMel(fMax)
        val melPoints = DoubleArray(nMels + 2) { i -> melMin + (melMax - melMin) * i / (nMels + 1) }
        val hzPoints = melPoints.map { melToHz(it) }
        val bin = hzPoints.map { kotlin.math.floor((nFft + 1) * it / sampleRate).toInt() }
        val filterbank = Array(nMels) { DoubleArray(nFft / 2) { 0.0 } }
        for (m in 1..nMels) {
            val f_m_minus = bin[m - 1]
            val f_m = bin[m]
            val f_m_plus = bin[m + 1]
            for (k in f_m_minus until f_m) {
                if (k in 0 until nFft / 2) {
                    filterbank[m - 1][k] = (k - f_m_minus).toDouble() / (f_m - f_m_minus)
                }
            }
            for (k in f_m until f_m_plus) {
                if (k in 0 until nFft / 2) {
                    filterbank[m - 1][k] = (f_m_plus - k).toDouble() / (f_m_plus - f_m)
                }
            }
        }
        return filterbank
    }
    private fun dct(input: DoubleArray, nCoeffs: Int): DoubleArray {
        val N = input.size
        val result = DoubleArray(nCoeffs)
        for (k in 0 until nCoeffs) {
            var sum = 0.0
            for (n in 0 until N) {
                sum += input[n] * kotlin.math.cos(Math.PI * k * (2 * n + 1) / (2.0 * N))
            }
            result[k] = sum * kotlin.math.sqrt(2.0 / N)
        }
        return result
    }

    fun extractMfccForFrame(
        floatFrame: FloatArray,
        sampleRate: Int = 8000,
        nMfcc: Int = 13,
        nMelBands: Int = 26
    ): FloatArray {
        val mfcc = MFCC(
            floatFrame.size,          // frameSize (nFFT)
            sampleRate.toFloat(),     // sampleRate
            nMfcc,                    // number of MFCCs
            nMelBands,                // number of Mel bands
            20f,                      // minFreq (Hz)
            (sampleRate / 2).toFloat() // maxFreq (Hz)
        )
        // Create the audio format (PCM, mono, 32-bit float)
        val audioFormat = TarsosDSPAudioFormat(
            sampleRate.toFloat(), // sample rate
            32,                   // sample size in bits
            1,                    // channels
            true,                 // signed
            false                 // bigEndian
        )
        val audioEvent = AudioEvent(audioFormat)
        audioEvent.setFloatBuffer(floatFrame)
        mfcc.process(audioEvent)
        return mfcc.mfcc
    }

    fun printMfccStats(fingerprint: List<FloatArray>) {
        if (fingerprint.isEmpty()) return
        val nMfcc = fingerprint[0].size - 4
        val mfccMatrix = Array(fingerprint.size) { FloatArray(nMfcc) }
        for (i in fingerprint.indices) {
            for (j in 0 until nMfcc) {
                mfccMatrix[i][j] = fingerprint[i][4 + j]
            }
        }
        for (j in 0 until nMfcc) {
            val col = mfccMatrix.map { it[j] }
            val mean = col.average()
            val std = Math.sqrt(col.map { (it - mean) * (it - mean) }.average())
            println("MFCC ${j+1}: mean = $mean, std = $std")
        }
    }
} 