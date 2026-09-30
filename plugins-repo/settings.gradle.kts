rootProject.name = "AutoCloudStreamPlugins"

// Auto-include every generated provider module (any dir that has a
// build.gradle.kts). Generated modules land here via the
// "Generate Provider" GitHub workflow (or --out plugins-repo).
val disabled = listOf<String>()

File(rootDir, ".").eachDir { dir ->
    if (!disabled.contains(dir.name) && File(dir, "build.gradle.kts").exists()) {
        include(dir.name)
    }
}

fun File.eachDir(block: (File) -> Unit) {
    listFiles()?.filter { it.isDirectory }?.forEach { block(it) }
}
