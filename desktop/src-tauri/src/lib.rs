// The desktop shell is intentionally thin: all control-plane traffic goes
// straight from the webview to the PHOEBE HTTP adapter (/api/v1) with the
// session token in a header — no Rust-side command surface to audit.
#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_opener::init())
        .run(tauri::generate_context!())
        .expect("error while running tauri application");
}
