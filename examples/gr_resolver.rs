//! Runs the `gr` resolver as its own executable, for the conformance table. Not installed by `cargo install`.

fn main() {
    let mut args = std::env::args();
    let invoked = args.next().unwrap_or_default();
    let name = std::path::Path::new(&invoked)
        .file_name()
        .map(|n| n.to_string_lossy().into_owned())
        .unwrap_or_default();
    let rest: Vec<String> = args.collect();
    std::process::exit(gitgrip::gr_resolver::run(&name, &rest));
}
