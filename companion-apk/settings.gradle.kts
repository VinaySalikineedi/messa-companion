// Messa Companion APK -- standalone Android Studio project
// (open-source-phone.md section 2/3, feature/messa-companion-apk)
//
// Deliberately its own top-level Gradle project, not a module folded into
// the Python `messa/` repo's own tooling -- this is the one piece of this
// feature that gets built by Android Studio / a real Android toolchain,
// never by anything in requirements.txt or the Python test suite.

pluginManagement {
    repositories {
        google()
        mavenCentral()
        gradlePluginPortal()
    }
}

dependencyResolutionManagement {
    repositoriesMode.set(RepositoriesMode.FAIL_ON_PROJECT_REPOS)
    repositories {
        google()
        mavenCentral()
    }
}

rootProject.name = "messa-companion"
include(":app")
