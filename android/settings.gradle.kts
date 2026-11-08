// Steno-Live for Android: the live shorthand helper of factcheck-steno, running on a tablet.
// core = plain Kotlin/JVM (logic + API client, unit-tested on the JVM); app = Android UI, capture, overlay.
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

rootProject.name = "steno-android"
include(":core", ":app")
