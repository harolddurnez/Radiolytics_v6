package com.example.radiolytics

import android.app.Application
import android.util.Log
import com.google.android.gms.common.ConnectionResult
import com.google.android.gms.common.GoogleApiAvailability
import com.google.firebase.FirebaseApp
import com.google.firebase.appcheck.FirebaseAppCheck
import com.google.firebase.appcheck.playintegrity.PlayIntegrityAppCheckProviderFactory

class RadiolyticsApp : Application() {
    companion object {
        private const val TAG = "RadiolyticsApp"
    }

    override fun onCreate() {
        super.onCreate()
        
        // Initialize Firebase
        FirebaseApp.initializeApp(this)
        
        // Initialize Firebase App Check with fallback
        val firebaseAppCheck = FirebaseAppCheck.getInstance()
        try {
            // Try to use Play Integrity first
            val playIntegrityFactory = PlayIntegrityAppCheckProviderFactory.getInstance()
            firebaseAppCheck.installAppCheckProviderFactory(playIntegrityFactory)
            Log.d(TAG, "Using Play Integrity App Check provider")
        } catch (e: Exception) {
            // Fall back to debug provider if Play Integrity is not available
            Log.w(TAG, "Play Integrity not available, falling back to debug provider", e)
            try {
                // Use reflection to get the debug provider
                val debugProviderClass = Class.forName("com.google.firebase.appcheck.debug.DebugAppCheckProviderFactory")
                val getInstanceMethod = debugProviderClass.getMethod("getInstance")
                val debugFactory = getInstanceMethod.invoke(null)
                firebaseAppCheck.installAppCheckProviderFactory(debugFactory as com.google.firebase.appcheck.AppCheckProviderFactory)
                Log.d(TAG, "Using Debug App Check provider")
            } catch (e2: Exception) {
                Log.e(TAG, "Failed to initialize debug provider", e2)
            }
        }
        
        // Check Google Play Services availability
        val googleApiAvailability = GoogleApiAvailability.getInstance()
        val resultCode = googleApiAvailability.isGooglePlayServicesAvailable(this)
        if (resultCode != ConnectionResult.SUCCESS) {
            // Google Play Services is not available
            googleApiAvailability.showErrorNotification(this, resultCode)
        }
    }
} 