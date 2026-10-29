fn main() {
    #[cfg(feature = "native-host")]
    tauri_build::build()
}
