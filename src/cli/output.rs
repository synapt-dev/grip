//! CLI output formatting
//!
//! Provides colored output, spinners, and formatting utilities.

use colored::Colorize;
use indicatif::{ProgressBar, ProgressStyle};
use std::time::Duration;

/// Output helper for consistent CLI formatting
pub struct Output;

impl Output {
    /// Print a success message
    pub fn success(message: &str) {
        println!("{} {}", "✓".green(), message);
    }

    /// Print an error message
    pub fn error(message: &str) {
        eprintln!("{} {}", "✗".red(), message);
    }

    /// Print a warning message
    pub fn warning(message: &str) {
        println!("{} {}", "⚠".yellow(), message);
    }

    /// Print an info message
    pub fn info(message: &str) {
        println!("{} {}", "ℹ".blue(), message);
    }

    /// Print a header
    pub fn header(message: &str) {
        println!("\n{}", message.bold());
    }

    /// Print a subheader
    pub fn subheader(message: &str) {
        println!("  {}", message.dimmed());
    }

    /// Print a key-value pair
    pub fn kv(key: &str, value: &str) {
        println!("  {}: {}", key.dimmed(), value);
    }

    /// Print a list item
    pub fn list_item(item: &str) {
        println!("  • {}", item);
    }

    /// Print a numbered list item
    pub fn numbered_item(num: usize, item: &str) {
        println!("  {}. {}", num, item);
    }

    /// Print a diff addition
    pub fn diff_add(line: &str) {
        println!("{}", format!("+ {}", line).green());
    }

    /// Print a diff deletion
    pub fn diff_del(line: &str) {
        println!("{}", format!("- {}", line).red());
    }

    /// Create a spinner with a message
    pub fn spinner(message: &str) -> ProgressBar {
        let pb = ProgressBar::new_spinner();
        pb.set_style(
            ProgressStyle::default_spinner()
                .template("{spinner:.cyan} {msg}")
                .expect("static spinner template is valid")
                .tick_chars("⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"),
        );
        pb.set_message(message.to_string());
        pb.enable_steady_tick(Duration::from_millis(80));
        pb
    }

    /// Create a progress bar
    pub fn progress_bar(total: u64, message: &str) -> ProgressBar {
        let pb = ProgressBar::new(total);
        pb.set_style(
            ProgressStyle::default_bar()
                .template("{msg} [{bar:40.cyan/blue}] {pos}/{len}")
                .expect("static progress bar template is valid")
                .progress_chars("█▓░"),
        );
        pb.set_message(message.to_string());
        pb
    }

    /// Format a repo name consistently
    pub fn repo_name(name: &str) -> String {
        name.cyan().bold().to_string()
    }

    /// Format a branch name consistently
    pub fn branch_name(name: &str) -> String {
        name.magenta().to_string()
    }

    /// Format a status (open, closed, merged)
    pub fn status(status: &str) -> String {
        match status.to_lowercase().as_str() {
            "open" => status.green().to_string(),
            "merged" => status.magenta().to_string(),
            "closed" => status.red().to_string(),
            "success" | "passed" => status.green().to_string(),
            "failure" | "failed" => status.red().to_string(),
            "pending" => status.yellow().to_string(),
            _ => status.to_string(),
        }
    }

    /// Format a URL as a clickable link (for terminals that support it)
    pub fn link(text: &str, url: &str) -> String {
        // OSC 8 hyperlink escape sequence
        format!("\x1b]8;;{}\x1b\\{}\x1b]8;;\x1b\\", url, text.underline())
    }
}

/// Table builder for formatted output
pub struct Table {
    headers: Vec<String>,
    rows: Vec<Vec<String>>,
    column_widths: Vec<usize>,
}

impl Table {
    /// Create a new table with headers
    pub fn new(headers: Vec<&str>) -> Self {
        let headers: Vec<String> = headers.into_iter().map(|s| s.to_string()).collect();
        let column_widths = headers.iter().map(|h| h.len()).collect();
        Self {
            headers,
            rows: Vec::new(),
            column_widths,
        }
    }

    /// Add a row to the table
    pub fn add_row(&mut self, row: Vec<&str>) {
        let row: Vec<String> = row.into_iter().map(|s| s.to_string()).collect();
        for (i, cell) in row.iter().enumerate() {
            if i < self.column_widths.len() {
                self.column_widths[i] = self.column_widths[i].max(cell.len());
            }
        }
        self.rows.push(row);
    }

    /// Print the table
    pub fn print(&self) {
        // Print headers
        let header_line: String = self
            .headers
            .iter()
            .enumerate()
            .map(|(i, h)| format!("{:width$}", h, width = self.column_widths[i]))
            .collect::<Vec<_>>()
            .join("  ");
        println!("{}", header_line.bold());

        // Print separator
        let sep_line: String = self
            .column_widths
            .iter()
            .map(|w| "-".repeat(*w))
            .collect::<Vec<_>>()
            .join("  ");
        println!("{}", sep_line.dimmed());

        // Print rows
        for row in &self.rows {
            let row_line: String = row
                .iter()
                .enumerate()
                .map(|(i, cell)| {
                    let width = self.column_widths.get(i).copied().unwrap_or(cell.len());
                    format!("{:width$}", cell, width = width)
                })
                .collect::<Vec<_>>()
                .join("  ");
            println!("{}", row_line);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_table() {
        let mut table = Table::new(vec!["Name", "Status", "Branch"]);
        table.add_row(vec!["repo1", "clean", "main"]);
        table.add_row(vec!["repo2", "modified", "feat/test"]);
        // Just verify it doesn't panic
        table.print();
    }
}
