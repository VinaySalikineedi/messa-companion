# Messa Companion APK -- release minification is off by default
# (isMinifyEnabled = false in app/build.gradle.kts), so this file is a
# placeholder for whoever turns it on for a production build. No custom
# rules are needed today: OkHttp/okio ship their own consumer-proguard
# rules, and this app has no reflection-based serialization to protect.
