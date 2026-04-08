use anyhow::Result;

use crate::args::Commands;

pub async fn dispatch_command(command: Commands, verbose: bool) -> Result<()> {
    match command {
        Commands::Doctor => {
            if verbose {
                println!("gr2 bootstrap OK (verbose)");
            } else {
                println!("gr2 bootstrap OK");
            }
            Ok(())
        }
    }
}
