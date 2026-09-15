// Messa Companion APK -- app module (open-source-phone.md section 2/3).
//
// Dependencies are deliberately minimal: AndroidX core/appcompat for the
// pairing UI, security-crypto for the Keystore-backed device secret
// (section 3.2), OkHttp for the WebSocket client (section 2's outbound
// wss:// connection -- no custom TLS/socket code, just OkHttp's own
// WebSocket implementation), and Kotlin coroutines for the relay loops.
// No ADB library dependency of any kind: this app never speaks the ADB
// protocol itself, it only ever relays raw bytes between two sockets
// (AdbLocalSocketRelay.kt) -- see that file's own header for why.
plugins {
    id("com.android.application")
    id("org.jetbrains.kotlin.android")
}

android {
    namespace = "ai.messa.companion"
    compileSdk = 34

    defaultConfig {
        applicationId = "ai.messa.companion"
        minSdk = 26   // NsdManager + EncryptedSharedPreferences both need this floor comfortably
        targetSdk = 34
        versionCode = 1
        versionName = "0.1.0"
    }

    buildTypes {
        release {
            isMinifyEnabled = false
            proguardFiles(getDefaultProguardFile("proguard-android-optimize.txt"), "proguard-rules.pro")
        }
    }

    compileOptions {
        sourceCompatibility = JavaVersion.VERSION_17
        targetCompatibility = JavaVersion.VERSION_17
    }

    kotlinOptions {
        jvmTarget = "17"
    }

    buildFeatures {
        viewBinding = false
    }
}

dependencies {
    implementation("androidx.core:core-ktx:1.13.1")
    implementation("androidx.appcompat:appcompat:1.7.0")
    implementation("com.google.android.material:material:1.12.0")
    implementation("androidx.constraintlayout:constraintlayout:2.1.4")
    implementation("androidx.lifecycle:lifecycle-service:2.8.4")
    implementation("androidx.lifecycle:lifecycle-runtime-ktx:2.8.4")
    implementation("androidx.activity:activity-ktx:1.9.1")

    // Android Keystore-backed EncryptedSharedPreferences -- the ONLY place
    // the raw device secret is ever stored on-device (section 3.2).
    implementation("androidx.security:security-crypto:1.1.0-alpha06")

    // Outbound WebSocket client for the `/device/ws` reverse tunnel.
    implementation("com.squareup.okhttp3:okhttp:4.12.0")

    // Coroutines for the relay loops (AdbLocalSocketRelay <-> WebSocketBridgeClient).
    implementation("org.jetbrains.kotlinx:kotlinx-coroutines-android:1.8.1")

    testImplementation("junit:junit:4.13.2")
    androidTestImplementation("androidx.test.ext:junit:1.2.1")
}
